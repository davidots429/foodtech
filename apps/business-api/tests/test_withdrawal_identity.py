import copy
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from botocore.exceptions import ReadTimeoutError

from foodtech_business_api.withdrawal_identity import CognitoWithdrawalRelease, WithdrawalIdentityConflict


class NotFound(Exception):
    pass


class WithdrawalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = 'opaque_cognito_subject-UpperCase', str(uuid4())
        self.now = int(time.time())
        self.account = {'pk': f'USER#{self.subject}', 'epoch': 4, 'suspended': True,
            'withdrawal_epoch': 1,
            'withdrawal_status': 'DELETING', 'withdrawal_request_id': self.request,
            'withdrawal_requested_at': self.now - 604801, 'withdrawal_deadline': self.now - 1,
            'deletion_lock_request': self.request}
        self.registry = {'pk': 'REGISTRY', 'sk': f'REQ#{self.request}',
                         'request_id': self.request, 'status': 'DELETING', 'version': 1,
                         'encrypted_user_id': b'synthetic-ciphertext', 'encrypted_capsule_locator': b'synthetic-ciphertext'}
        self.provider_sub = self.subject
        self.deletes, self.writes, self.gets = [], [], []
        self.timeout = False
        self.keep_identity = False
        self.client = SimpleNamespace(transact_get_items=self.read, transact_write_items=self.write)
        self.table = SimpleNamespace(name='auth', meta=SimpleNamespace(client=self.client))
        self.cognito = SimpleNamespace(exceptions=SimpleNamespace(UserNotFoundException=NotFound),
                                       admin_get_user=self.get_user, admin_delete_user=self.delete_user)
        self.worker = CognitoWithdrawalRelease(auth_table=self.table, registry_table='registry',
            enrollment=SimpleNamespace(table=self.table, fence=lambda: 0), cognito_client=self.cognito,
            pool_id='ap-southeast-2_synthetic', clock=lambda: self.now)

    def read(self, **_):
        return {'Responses': [{'Item': copy.deepcopy(self.account)}, {'Item': copy.deepcopy(self.registry)}]}

    def write(self, **kwargs):
        self.writes.append(kwargs)

    def get_user(self, **kwargs):
        self.gets.append(kwargs)
        if self.provider_sub is None:
            raise NotFound()
        return {'Username': 'reusable@example.invalid', 'UserAttributes': [{'Name': 'sub', 'Value': self.provider_sub}]}

    def delete_user(self, **kwargs):
        self.deletes.append(kwargs)
        if not self.keep_identity:
            self.provider_sub = None
        if self.timeout:
            raise ReadTimeoutError(endpoint_url='https://synthetic.invalid')

    def release(self):
        return self.worker.release(subject=self.subject, request_id=self.request, expected_epoch=4)

    def test_deletes_exact_opaque_subject_without_reusing_email_or_reporting_complete(self):
        self.assertEqual(self.release(), {'state': 'COGNITO_REMOVED', 'deletion_complete': False})
        self.assertEqual(self.deletes, [{'UserPoolId': 'ap-southeast-2_synthetic', 'Username': self.subject}])
        self.assertTrue(all(call['Username'] == self.subject for call in self.gets))
        update, check = self.writes[0]['TransactItems']
        self.assertIn('withdrawal_deadline = :deadline', update['Update']['ConditionExpression'])
        self.assertEqual(check['ConditionCheck']['ExpressionAttributeValues'][':deleting'], 'DELETING')
        self.assertEqual(self.account['deletion_lock_request'], self.request)

    def test_pending_cancelled_future_or_wrong_request_never_calls_cognito(self):
        original = copy.deepcopy(self.account)
        for change in ({'withdrawal_status': 'PENDING'}, {'withdrawal_status': 'CANCELED'},
                       {'withdrawal_deadline': self.now + 1}, {'deletion_lock_request': str(uuid4())},
                       {'suspended': False}, {'epoch': 5}):
            self.account = {**original, **change}
            with self.subTest(change=change), self.assertRaises(WithdrawalIdentityConflict):
                self.release()
        self.assertEqual((self.gets, self.deletes, self.writes), ([], [], []))

    def test_foreign_provider_sub_is_rejected_without_deleting(self):
        self.provider_sub = 'another-subject'
        with self.assertRaises(WithdrawalIdentityConflict):
            self.release()
        self.assertEqual((self.deletes, self.writes), ([], []))

    def test_deleted_identity_response_loss_can_reconcile_without_second_delete(self):
        self.timeout = True
        with self.assertRaises(ReadTimeoutError):
            self.release()
        self.assertEqual(self.writes, [])
        self.timeout = False
        self.assertEqual(self.release()['state'], 'COGNITO_REMOVED')
        self.assertEqual(len(self.deletes), 1)

    def test_absence_not_confirmed_does_not_record_retirement(self):
        self.keep_identity = True
        with self.assertRaises(WithdrawalIdentityConflict):
            self.release()
        self.assertEqual(self.writes, [])

    def test_authorization_change_before_irreversible_call_rejects_operation(self):
        original = self.get_user
        def changed(**kwargs):
            user = original(**kwargs)
            self.registry['status'] = 'RECONCILING'
            return user
        self.cognito.admin_get_user = changed
        with self.assertRaises(WithdrawalIdentityConflict):
            self.release()
        self.assertEqual((self.deletes, self.writes), ([], []))


if __name__ == '__main__':
    unittest.main()
