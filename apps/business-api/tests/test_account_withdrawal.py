import copy
import unittest
from decimal import Decimal
from uuid import uuid4

import boto3
from botocore.stub import ANY, Stubber

from foodtech_business_api.account_withdrawal import (
    GRACE_SECONDS, DynamoWithdrawalStore, WithdrawalConflict, withdrawal_change,
)


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.now = 1_800_000_000
        self.active = {'pk': f'USER#{self.subject}', 'epoch': 4, 'admin_blocked': True}

    def change(self, record, action, **overrides):
        args = dict(subject=self.subject, request_id=self.request, now=self.now, action=action)
        args.update(overrides)
        return withdrawal_change(record, **args)

    def pending(self):
        return {**self.active, **self.change(self.active, 'request')}

    def test_request_revokes_sessions_and_preserves_other_restrictions(self):
        pending = self.pending()
        self.assertEqual(pending['epoch'], 5)
        self.assertIs(pending['suspended'], True)
        self.assertIs(pending['admin_blocked'], True)
        self.assertEqual(pending['withdrawal_deadline'], self.now + 604800)
        self.assertIsNone(self.change(pending, 'request', now=self.now + 100))
        self.assertEqual(pending['withdrawal_requested_at'], self.now)
        with self.assertRaises(WithdrawalConflict):
            self.change(pending, 'request', request_id=str(uuid4()))

    def test_cancel_before_deadline_and_expire_at_deadline_are_exclusive(self):
        pending = self.pending()
        deadline = pending['withdrawal_deadline']
        canceled = {**pending, **self.change(pending, 'cancel', now=deadline - 1)}
        self.assertEqual((canceled['withdrawal_status'], canceled['epoch'], canceled['suspended']),
                         ('CANCELED', 6, False))
        self.assertTrue(canceled['admin_blocked'])
        self.assertIsNone(self.change(canceled, 'cancel', now=deadline))
        with self.assertRaises(WithdrawalConflict):
            self.change(canceled, 'expire', now=deadline)
        with self.assertRaises(WithdrawalConflict):
            self.change(pending, 'expire', now=deadline - 1)
        with self.assertRaises(WithdrawalConflict):
            self.change(pending, 'cancel', now=deadline)
        deleting = {**pending, **self.change(pending, 'expire', now=deadline)}
        self.assertEqual((deleting['withdrawal_status'], deleting['epoch'], deleting['suspended']),
                         ('DELETING', 6, True))
        self.assertIsNone(self.change(deleting, 'expire', now=deadline + 1))
        with self.assertRaises(WithdrawalConflict):
            self.change(deleting, 'cancel', now=deadline + 1)

    def test_missing_account_or_admin_suspension_cannot_be_recreated_or_canceled(self):
        for record in (None, {}, {**self.active, 'suspended': True},
                       {**self.active, 'deletion_lock_request': str(uuid4())}):
            with self.subTest(record=record), self.assertRaises(WithdrawalConflict):
                self.change(record, 'request')
        pending = self.pending()
        with self.assertRaises(WithdrawalConflict):
            self.change({**pending, 'epoch': pending['epoch'] + 1}, 'cancel')
        with self.assertRaises(WithdrawalConflict):
            self.change({**pending, 'deletion_lock_request': self.request}, 'cancel')

    def test_new_request_after_cancel_has_new_id_and_deadline(self):
        pending = self.pending()
        canceled = {**pending, **self.change(pending, 'cancel')}
        with self.assertRaises(WithdrawalConflict):
            self.change(canceled, 'request')
        new_id = str(uuid4())
        fresh = self.change(canceled, 'request', request_id=new_id, now=self.now + 10)
        self.assertEqual(fresh['withdrawal_deadline'], self.now + 10 + GRACE_SECONDS)
        self.assertEqual(fresh['withdrawal_request_id'], new_id)
        with self.assertRaises(WithdrawalConflict):
            self.change({**canceled, **fresh}, 'cancel')

    def test_corrupt_state_and_foreign_identity_fail_closed(self):
        pending = self.pending()
        for change in ({'epoch': True}, {'epoch': Decimal('1.5')}, {'epoch': Decimal('NaN')},
                       {'withdrawal_deadline': pending['withdrawal_deadline'] + 1},
                       {'withdrawal_requested_at': '1800000000'}, {'withdrawal_epoch': 0},
                       {'pk': f'USER#{uuid4()}'}):
            with self.subTest(change=change), self.assertRaises(WithdrawalConflict):
                self.change({**pending, **change}, 'cancel')
        for now in (True, -1, 1.5):
            with self.assertRaises(ValueError):
                self.change(self.active, 'request', now=now)

    def test_dynamo_transition_uses_strong_read_and_conditional_update(self):
        resource = boto3.resource('dynamodb', region_name='ap-southeast-2',
                                 aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        table = resource.Table('synthetic-auth')
        client = resource.meta.client
        captured = []
        # Capture native parameters before the resource marshals them.
        original_update = table.update_item
        def update(**kwargs):
            captured.append(copy.deepcopy(kwargs))
            return original_update(**kwargs)
        table.update_item = update
        with Stubber(client) as stub:
            stub.add_response('get_item', {'Item': {'pk': {'S': self.active['pk']}, 'epoch': {'N': '4'}}},
                              {'TableName': table.name, 'Key': {'pk': self.active['pk']}, 'ConsistentRead': True})
            stub.add_client_error('update_item', service_error_code='ConditionalCheckFailedException',
                                  expected_params={'TableName': table.name, 'Key': ANY,
                                      'UpdateExpression': ANY, 'ConditionExpression': ANY,
                                      'ExpressionAttributeNames': ANY, 'ExpressionAttributeValues': ANY,
                                      'ReturnValues': 'ALL_NEW'})
            with self.assertRaisesRegex(WithdrawalConflict, 'account changed'):
                DynamoWithdrawalStore(table=table).transition(subject=self.subject, request_id=self.request,
                    now=self.now, action='request')
            stub.assert_no_pending_responses()
        write = captured[0]
        self.assertIn('attribute_exists(pk)', write['ConditionExpression'])
        self.assertIn('#epoch = :observed_epoch', write['ConditionExpression'])
        self.assertIn('attribute_not_exists(deletion_lock_request)', write['ConditionExpression'])
        self.assertEqual(write['ExpressionAttributeValues'][':observed_epoch'], 4)
        for field in ('suspended', 'withdrawal_status', 'withdrawal_request_id', 'withdrawal_deadline'):
            self.assertIn(field, write['ExpressionAttributeNames'].values())


if __name__ == '__main__':
    unittest.main()
