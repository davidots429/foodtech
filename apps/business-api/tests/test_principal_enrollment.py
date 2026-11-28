import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import boto3
from botocore.stub import ANY, Stubber
from fastapi import HTTPException

from foodtech_business_api import auth
from foodtech_business_api.auth_store import MemoryStore
from foodtech_business_api.principal_enrollment import (
    DynamoPrincipalEnrollment, DynamoPrincipalRetirement, PrincipalEnrollmentConflict,
)
from test_auth import ENV


class LoginEnrollmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {**ENV, 'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true'})
        self.environment.start()
        self.store, self.subject = MemoryStore(), str(uuid4())
        self.settings = auth.Settings.from_env()

    async def asyncTearDown(self):
        self.environment.stop()

    async def test_fresh_identity_check_enrolls_missing_account(self):
        observed = []
        original = self.store.enrollment_fence
        def fence():
            observed.append('fence')
            return original()
        def cognito(*_):
            observed.append('cognito')
            self.assertIsNone(self.store.principal_record(self.subject))
            return True
        self.store.enrollment_fence = fence
        with patch.object(auth, '_cognito_access', side_effect=cognito):
            record = await auth._login_principal(self.settings, self.store, self.subject, 'customer')
        self.assertEqual(observed, ['fence', 'cognito'])
        self.assertEqual(record, {'pk': f'USER#{self.subject}', 'epoch': 0, 'suspended': False})

    async def test_retirement_between_identity_check_and_initial_put_rejects_old_generation(self):
        def retired(*_):
            self.store.enrollment_generation += 1
            return True
        with patch.object(auth, '_cognito_access', side_effect=retired):
            with self.assertRaises(HTTPException) as error:
                await auth._login_principal(self.settings, self.store, self.subject, 'customer')
        self.assertEqual(error.exception.status_code, 409)
        self.assertIsNone(self.store.principal_record(self.subject))

    async def test_deleted_identity_and_existing_locked_account_are_not_recreated(self):
        with patch.object(auth, '_cognito_access', return_value=False):
            with self.assertRaises(HTTPException) as error:
                await auth._login_principal(self.settings, self.store, self.subject, 'customer')
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.store.principals, {})
        self.store.principals[self.subject] = {'epoch': 3, 'suspended': True, 'deletion_lock_request': str(uuid4())}
        with patch.object(auth, '_cognito_access', return_value=True):
            with self.assertRaises(HTTPException):
                await auth._login_principal(self.settings, self.store, self.subject, 'customer')
        self.assertEqual(self.store.principal(self.subject)['epoch'], 3)

    async def test_old_subject_stays_absent_while_new_subject_can_enroll(self):
        self.store.enrollment_generation = 1
        new_subject = str(uuid4())
        with patch.object(auth, '_cognito_access', side_effect=lambda settings, subject, surface: subject == new_subject):
            with self.assertRaises(HTTPException):
                await auth._login_principal(self.settings, self.store, self.subject, 'customer')
            record = await auth._login_principal(self.settings, self.store, new_subject, 'customer')
        self.assertEqual(record['pk'], f'USER#{new_subject}')
        self.assertNotIn(self.subject, self.store.principals)


class EnrollmentWireTests(unittest.TestCase):
    def setUp(self):
        self.resource = boto3.resource('dynamodb', region_name='ap-southeast-2',
                                       aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        self.table = self.resource.Table('synthetic-auth')
        self.subject, self.request = str(uuid4()), str(uuid4())

    def test_missing_fence_is_not_automatically_bootstrapped(self):
        store = DynamoPrincipalEnrollment(table=self.table)
        with Stubber(self.resource.meta.client) as stub:
            stub.add_response('get_item', {}, {'TableName': self.table.name,
                'Key': {'pk': store.fence_key}, 'ConsistentRead': True})
            with self.assertRaises(PrincipalEnrollmentConflict):
                store.fence()
            stub.assert_no_pending_responses()

    def test_initial_update_is_conditioned_on_generation_and_nonexistence(self):
        store = DynamoPrincipalEnrollment(table=self.table)
        observed = []
        original = self.resource.meta.client.transact_write_items
        def capture(**kwargs):
            observed.append(kwargs)
            return original(**kwargs)
        self.resource.meta.client.transact_write_items = capture
        with Stubber(self.resource.meta.client) as stub:
            stub.add_client_error('transact_write_items', service_error_code='TransactionCanceledException',
                                  expected_params={'TransactItems': ANY})
            with self.assertRaises(PrincipalEnrollmentConflict):
                store.enroll(subject=self.subject, expected_generation=4)
            stub.assert_no_pending_responses()
        check, update = observed[0]['TransactItems']
        self.assertEqual(check['ConditionCheck']['ExpressionAttributeValues'][':observed'], 4)
        self.assertEqual(update['Update']['Key'], {'pk': f'USER#{self.subject}'})
        self.assertEqual(update['Update']['ConditionExpression'], 'attribute_not_exists(pk)')
        self.assertEqual(update['Update']['ExpressionAttributeNames'], {'#epoch': 'epoch'})
        self.assertEqual(update['Update']['ExpressionAttributeValues'], {':initial': 0})

    def test_cognito_identity_must_be_absent_before_retirement_transaction(self):
        class NotFound(Exception):
            pass
        cognito = SimpleNamespace(exceptions=SimpleNamespace(UserNotFoundException=NotFound), admin_get_user=lambda **_: {})
        retire = DynamoPrincipalRetirement(auth_table=self.table, registry_table='synthetic-registry',
                                           cognito_client=cognito, pool_id='synthetic-pool')
        with patch.object(retire.enrollment, 'fence', return_value=0):
            with self.assertRaisesRegex(PrincipalEnrollmentConflict, 'has not been removed'):
                retire.retire(subject=self.subject, request_id=self.request, expected_epoch=2)

    def test_retirement_atomically_advances_fence_removes_subject_and_checks_completion(self):
        class NotFound(Exception):
            pass
        def absent(**_):
            raise NotFound()
        cognito = SimpleNamespace(exceptions=SimpleNamespace(UserNotFoundException=NotFound), admin_get_user=absent)
        retire = DynamoPrincipalRetirement(auth_table=self.table, registry_table='synthetic-registry',
                                           cognito_client=cognito, pool_id='synthetic-pool')
        seen = []
        snapshot = {'Responses': [{'Item': {'pk': f'USER#{self.subject}', 'epoch': 2, 'suspended': True,
            'deletion_lock_request': self.request}}, {'Item': {'pk': 'REGISTRY', 'sk': f'REQ#{self.request}',
            'request_id': self.request, 'status': 'COMPLETED'}}]}
        with patch.object(retire.enrollment, 'fence', return_value=3), \
                patch.object(self.resource.meta.client, 'transact_get_items', return_value=snapshot), \
                patch.object(self.resource.meta.client, 'transact_write_items', side_effect=lambda **kwargs: seen.append(kwargs)):
            retire.retire(subject=self.subject, request_id=self.request, expected_epoch=2)
        update, delete, check = seen[0]['TransactItems']
        self.assertEqual(update['Update']['ExpressionAttributeValues'][':next'], 4)
        self.assertEqual(delete['Delete']['Key']['pk'], f'USER#{self.subject}')
        self.assertIn('attribute_not_exists(#grants)', delete['Delete']['ConditionExpression'])
        self.assertEqual(delete['Delete']['ExpressionAttributeNames']['#grants'], 'source_read_grants')
        self.assertNotIn(':observed_grants', delete['Delete']['ExpressionAttributeValues'])
        self.assertEqual(check['ConditionCheck']['ExpressionAttributeValues'][':completed'], 'COMPLETED')

    def test_nonempty_references_survive_until_all_catalog_rows_are_absent(self):
        class NotFound(Exception):
            pass
        def absent(**_):
            raise NotFound()
        table = self.resource.Table('foodtech-auth-sessions-dev')
        retire = DynamoPrincipalRetirement(auth_table=table, registry_table='foodtech-deletion-registry-dev',
            cognito_client=SimpleNamespace(exceptions=SimpleNamespace(UserNotFoundException=NotFound), admin_get_user=absent),
            pool_id='synthetic-pool', source_catalog_table='foodtech-source-access-dev')
        inventory_check = {'ConditionCheck': {'TableName': 'synthetic-progress',
            'Key': {'pk': 'REQ#' + self.request, 'sk': 'DOMAIN#SOURCE_GRANT_INVENTORY'},
            'ConditionExpression': 'encrypted_checkpoint = :expected',
            'ExpressionAttributeValues': {':expected': b'encrypted-inventory'}}}
        inventory = SimpleNamespace(catalog_table='foodtech-source-access-dev',
            store=SimpleNamespace(auth_table=table.name, registry_table='foodtech-deletion-registry-dev'),
            retirement_conditions=lambda **_: [inventory_check])
        retire.source_inventory = inventory
        grants = {'JEYUK#jeyuk-' + str(index): str(uuid4()) for index in range(101)}
        snapshot = {'Responses': [{'Item': {'pk': 'USER#' + self.subject, 'epoch': 2, 'suspended': True,
            'deletion_lock_request': self.request, 'source_read_grants': grants}}, {'Item': {'pk': 'REGISTRY',
            'sk': 'REQ#' + self.request, 'request_id': self.request, 'status': 'COMPLETED'}}]}
        for present in (True, False):
            responses = [snapshot, {'Responses': ([{'Item': {'pk': 'GRANT#residual'}}] + [{}] * 99
                                               if present else [{}] * 100)}]
            if not present:
                responses.append({'Responses': [{}]})
            with patch.object(retire.enrollment, 'fence', return_value=3), \
                    patch.object(table.meta.client, 'transact_get_items', side_effect=responses) as read, \
                    patch.object(table.meta.client, 'transact_write_items') as write:
                if present:
                    with self.assertRaisesRegex(PrincipalEnrollmentConflict, 'residual'):
                        retire.retire(subject=self.subject, request_id=self.request, expected_epoch=2)
                    write.assert_not_called()
                else:
                    retire.retire(subject=self.subject, request_id=self.request, expected_epoch=2)
                    self.assertEqual(read.call_count, 3)
                    delete = write.call_args.kwargs['TransactItems'][1]['Delete']
                    self.assertEqual(delete['ExpressionAttributeValues'][':observed_grants'], grants)
                    self.assertIn('#grants = :observed_grants', delete['ConditionExpression'])
                    self.assertNotIn('UpdateExpression', delete)
                    self.assertEqual(write.call_args.kwargs['TransactItems'][-1], inventory_check)
        self.assertEqual(len(snapshot['Responses'][0]['Item']['source_read_grants']), 101)
        retire.source_inventory = None
        with patch.object(retire.enrollment, 'fence', return_value=3), \
                patch.object(table.meta.client, 'transact_get_items', return_value=snapshot), \
                patch.object(table.meta.client, 'transact_write_items') as write:
            with self.assertRaisesRegex(PrincipalEnrollmentConflict, 'verification required'):
                retire.retire(subject=self.subject, request_id=self.request, expected_epoch=2)
            write.assert_not_called()


class CognitoSubjectBindingTests(unittest.TestCase):
    def test_same_username_with_different_sub_is_not_the_original_identity(self):
        subject, different = str(uuid4()), str(uuid4())
        client = SimpleNamespace(admin_get_user=lambda **_: {'Enabled': True, 'UserStatus': 'CONFIRMED',
            'UserAttributes': [{'Name': 'sub', 'Value': different}]},
            exceptions=SimpleNamespace(UserNotFoundException=type('NotFound', (Exception,), {})))
        with patch.object(auth.boto3, 'client', return_value=client):
            self.assertFalse(auth._cognito_access(SimpleNamespace(issuer='https://synthetic/pool'), subject, 'customer'))
