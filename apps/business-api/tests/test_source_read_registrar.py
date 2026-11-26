"""Local SDK contract tests; no live AWS, grants or IAM changes."""
import copy
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.stub import Stubber

from foodtech_business_api.source_read_registrar import DynamoSourceReadRegistrar, SourceGrantConflict


class SourceReadRegistrarTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.Session(region_name='ap-southeast-2', aws_access_key_id='synthetic',
            aws_secret_access_key='synthetic').client('dynamodb')
        self.stub = Stubber(self.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.row = {'pk': 'USER#opaque_subject', 'epoch': 3, 'suspended': False}
        self.grant_id = str(uuid4())
        self.registrar = DynamoSourceReadRegistrar(auth_table='foodtech-auth-sessions-dev',
            catalog_table='foodtech-source-access-dev', client=self.client, clock=lambda: 1000)
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()

    def snapshot(self, row=None):
        item = self.row if row is None else row
        self.stub.add_response('get_item', {'Item': {k: self.serializer.serialize(v) for k, v in item.items()}},
            {'TableName': 'foodtech-auth-sessions-dev', 'Key': {'pk': {'S': 'USER#opaque_subject'}},
             'ConsistentRead': True})

    def grant(self, **changes):
        arguments = dict(subject='opaque_subject', expected_epoch=3, source_partition='JEYUK#jeyuk-current',
                         grant_id=self.grant_id, expires_at=2000)
        return self.registrar.grant(**{**arguments, **changes})

    def decoded(self, values):
        return {key: self.decoder.deserialize(value) for key, value in values.items()}

    def test_grant_is_atomic_and_catalog_does_not_copy_subject(self):
        self.snapshot()
        writes = []
        self.stub.add_response('transact_write_items', {})
        self.client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
            lambda params, **kwargs: writes.append(copy.deepcopy(params)))
        self.assertEqual(self.grant(), self.grant_id)
        self.stub.assert_no_pending_responses()
        request = writes[0]
        self.assertEqual(request['ClientRequestToken'], self.grant_id)
        update, put = request['TransactItems']
        update = update['Update']
        self.assertIn('attribute_not_exists(#grants)', update['ConditionExpression'])
        self.assertIn('attribute_not_exists(deletion_lock_request)', update['ConditionExpression'])
        self.assertIn('#epoch = :epoch', update['ConditionExpression'])
        self.assertEqual(self.decoded(update['ExpressionAttributeValues'])[':after'],
                         {'JEYUK#jeyuk-current': self.grant_id})
        catalog = self.decoded(put['Put']['Item'])
        self.assertEqual(catalog['permission'], 'RECIPE_SOURCE_READ')
        self.assertEqual(catalog['expires_at'], 2000)
        self.assertNotIn('opaque_subject', repr(catalog))
        self.assertEqual(put['Put']['ConditionExpression'], 'attribute_not_exists(pk)')

    def test_existing_grants_are_preserved_and_snapshot_is_fenced(self):
        prior = str(uuid4())
        self.row['source_read_grants'] = {'JEYUK#jeyuk-prior': prior}
        self.snapshot()
        self.client.transact_write_items = MagicMock()
        self.grant()
        update = self.client.transact_write_items.call_args.kwargs['TransactItems'][0]['Update']
        values = self.decoded(update['ExpressionAttributeValues'])
        self.assertIn('#grants = :before', update['ConditionExpression'])
        self.assertEqual(values[':before'], {'JEYUK#jeyuk-prior': prior})
        self.assertEqual(values[':after']['JEYUK#jeyuk-prior'], prior)

    def test_deleted_suspended_locked_wrong_epoch_or_corrupt_accounts_never_write(self):
        for row in [{}, {**self.row, 'epoch': 4}, {**self.row, 'epoch': True},
                    {**self.row, 'suspended': True}, {**self.row, 'deletion_lock_request': 'request'},
                    {**self.row, 'source_read_grants': {'JEYUK#jeyuk-current': []}},
                    {**self.row, 'source_read_grants': {'foreign': str(uuid4())}}]:
            with self.subTest(row=row):
                self.snapshot(row)
                with self.assertRaises(SourceGrantConflict):
                    self.grant()
        self.stub.assert_no_pending_responses()

    def test_never_replaces_existing_binding_or_reuses_reference(self):
        for mapping in [{'JEYUK#jeyuk-current': str(uuid4())}, {'JEYUK#jeyuk-prior': self.grant_id}]:
            self.snapshot({**self.row, 'source_read_grants': mapping})
            with self.assertRaises(SourceGrantConflict):
                self.grant()
        self.stub.assert_no_pending_responses()

    def test_invalid_or_elapsed_expiry_never_issues_write(self):
        for expires in [True, 1000, 0, 2000.0, 2**63]:
            with self.assertRaises(ValueError):
                self.grant(expires_at=expires)
        self.snapshot()
        times = iter([1000, 2000])
        self.registrar.clock = lambda: next(times)
        with self.assertRaises(SourceGrantConflict):
            self.grant()
        self.stub.assert_no_pending_responses()

    def test_transaction_race_fails_without_partial_fallback_or_retry(self):
        self.snapshot()
        self.stub.add_client_error('transact_write_items', 'TransactionCanceledException',
            service_message='synthetic conflict', http_status_code=400)
        with self.assertRaisesRegex(SourceGrantConflict, '^source grant changed$'):
            self.grant()
        self.stub.assert_no_pending_responses()

    def test_revoke_requires_exact_grant_and_removes_only_its_reference(self):
        prior = str(uuid4())
        self.row['source_read_grants'] = {'JEYUK#jeyuk-current': self.grant_id, 'JEYUK#jeyuk-prior': prior}
        self.snapshot()
        self.client.transact_write_items = MagicMock()
        self.registrar.revoke(subject='opaque_subject', expected_epoch=3,
            source_partition='JEYUK#jeyuk-current', expected_grant_id=self.grant_id, operation_id=str(uuid4()))
        update, delete = self.client.transact_write_items.call_args.kwargs['TransactItems']
        values = self.decoded(update['Update']['ExpressionAttributeValues'])
        self.assertEqual(values[':after'], {'JEYUK#jeyuk-prior': prior})
        self.assertEqual(self.decoded(delete['Delete']['Key']), {'pk': 'GRANT#' + self.grant_id, 'sk': 'META'})
        self.assertIn('attribute_not_exists(pk) OR', delete['Delete']['ConditionExpression'])
        self.assertEqual(delete['Delete']['ReturnValuesOnConditionCheckFailure'], 'NONE')
        self.snapshot()
        with self.assertRaises(SourceGrantConflict):
            self.registrar.revoke(subject='opaque_subject', expected_epoch=3,
                source_partition='JEYUK#jeyuk-current', expected_grant_id=prior, operation_id=str(uuid4()))
        self.assertEqual(self.client.transact_write_items.call_count, 1)

    def test_no_default_credentials_or_customer_route_is_created(self):
        with self.assertRaises(ValueError):
            DynamoSourceReadRegistrar(auth_table='foodtech-auth-sessions-dev',
                catalog_table='foodtech-source-access-demo', client=self.client, clock=lambda: 1000)
