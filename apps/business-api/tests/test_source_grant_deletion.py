"""Locked grant cleanup wire contracts; AWS calls are stubbed."""
import copy
import unittest
from uuid import uuid4
from unittest.mock import MagicMock

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.stub import Stubber

from foodtech_business_api.account_withdrawal import GRACE_SECONDS
from foodtech_business_api.source_grant_deletion import DynamoSourceGrantDeletion
from foodtech_business_api.source_read_registrar import SourceGrantConflict


class SourceGrantDeletionTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.Session(region_name='ap-southeast-2', aws_access_key_id='synthetic',
            aws_secret_access_key='synthetic').client('dynamodb')
        self.stub = Stubber(self.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.request = str(uuid4())
        self.refs = {'JEYUK#jeyuk-first': str(uuid4()), 'JEYUK#jeyuk-second': str(uuid4())}
        self.account = {'pk': 'USER#opaque_subject', 'epoch': 3, 'withdrawal_epoch': 2,
            'suspended': True, 'deletion_lock_request': self.request, 'withdrawal_request_id': self.request,
            'withdrawal_status': 'DELETING', 'withdrawal_requested_at': 1000,
            'withdrawal_deadline': 1000 + GRACE_SECONDS, 'source_read_grants': self.refs.copy()}
        self.registry = {'pk': 'REGISTRY', 'sk': 'REQ#' + self.request,
                         'request_id': self.request, 'status': 'DELETING'}
        self.worker = DynamoSourceGrantDeletion(auth_table='foodtech-auth-sessions-dev',
            catalog_table='foodtech-source-access-dev', registry_table='foodtech-deletion-registry-dev',
            client=self.client, clock=lambda: 1000 + GRACE_SECONDS)
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()

    def encode(self, row):
        return {key: self.serializer.serialize(value) for key, value in row.items()}

    def snapshot(self, account=None, registry=None):
        self.stub.add_response('transact_get_items', {'Responses': [
            {'Item': self.encode(self.account if account is None else account)},
            {'Item': self.encode(self.registry if registry is None else registry)}]},
            {'TransactItems': [
                {'Get': {'TableName': 'foodtech-auth-sessions-dev', 'Key': {'pk': {'S': 'USER#opaque_subject'}}}},
                {'Get': {'TableName': 'foodtech-deletion-registry-dev',
                         'Key': self.encode({'pk': 'REGISTRY', 'sk': 'REQ#' + self.request})}}]})

    def step(self):
        return self.worker.step(subject='opaque_subject', request_id=self.request, expected_epoch=3)

    def test_reference_and_catalog_delete_share_lock_epoch_registry_and_mapping_fences(self):
        self.snapshot()
        writes = []
        self.stub.add_response('transact_write_items', {})
        self.client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
            lambda params, **_: writes.append(copy.deepcopy(params)))
        self.assertEqual(self.step(), {'state': 'GRANT_REFERENCE_REMOVED', 'remaining': 1, 'deletion_complete': False})
        update, registry, delete = writes[0]['TransactItems']
        conditions = update['Update']['ConditionExpression']
        for condition in ('#epoch = :epoch', 'suspended = :yes', 'deletion_lock_request = :request',
                          'withdrawal_status = :deleting', '#grants = :before', 'withdrawal_deadline = :deadline'):
            self.assertIn(condition, conditions)
        values = {key: self.decoder.deserialize(value)
                  for key, value in update['Update']['ExpressionAttributeValues'].items()}
        self.assertEqual(values[':before'], self.refs)
        self.assertEqual(values[':after'], {'JEYUK#jeyuk-second': self.refs['JEYUK#jeyuk-second']})
        self.assertEqual(registry['ConditionCheck']['TableName'], 'foodtech-deletion-registry-dev')
        self.assertEqual(delete['Delete']['TableName'], 'foodtech-source-access-dev')
        self.assertEqual(delete['Delete']['Key']['pk'], {'S': 'GRANT#' + self.refs['JEYUK#jeyuk-first']})
        self.assertIn('attribute_not_exists(pk) OR', delete['Delete']['ConditionExpression'])
        for operation in writes[0]['TransactItems']:
            self.assertEqual(next(iter(operation.values()))['ReturnValuesOnConditionCheckFailure'], 'NONE')
        self.stub.assert_no_pending_responses()

    def test_invalid_lock_schedule_epoch_mapping_or_registry_never_writes(self):
        cases = [({'pk': 'USER#another'}, {}), ({'suspended': False}, {}), ({'epoch': 4}, {}), ({'withdrawal_epoch': 3}, {}),
                 ({'withdrawal_status': 'PENDING'}, {}), ({'withdrawal_status': 'CANCELED'}, {}),
                 ({'deletion_lock_request': str(uuid4())}, {}), ({'withdrawal_deadline': 1001 + GRACE_SECONDS}, {}),
                 ({'source_read_grants': {'foreign': str(uuid4())}}, {}),
                 ({'source_read_grants': {'JEYUK#jeyuk-first': self.refs['JEYUK#jeyuk-first'],
                                          'JEYUK#jeyuk-second': self.refs['JEYUK#jeyuk-first']}}, {}),
                 ({'source_read_grants': {'JEYUK#jeyuk-first': []}}, {}), ({}, {'status': 'COMPLETED'}),
                 ({}, {'request_id': str(uuid4())})]
        self.client.transact_write_items = MagicMock()
        for account, registry in cases:
            with self.subTest(account=account, registry=registry):
                self.snapshot({**self.account, **account}, {**self.registry, **registry})
                with self.assertRaises(SourceGrantConflict):
                    self.step()
        self.client.transact_write_items.assert_not_called()

    def test_grace_period_and_empty_mapping_do_not_manufacture_erasure_completion(self):
        self.worker.clock = lambda: 999 + GRACE_SECONDS
        self.snapshot()
        with self.assertRaises(SourceGrantConflict):
            self.step()
        self.worker.clock = lambda: 1000 + GRACE_SECONDS
        self.snapshot({**self.account, 'source_read_grants': {}})
        self.client.transact_write_items = MagicMock()
        self.assertEqual(self.step(), {'state': 'REFERENCES_EMPTY', 'remaining': 0, 'deletion_complete': False})
        self.client.transact_write_items.assert_not_called()

    def test_conflict_is_not_retried_and_replay_uses_same_operation_token(self):
        self.snapshot()
        self.stub.add_client_error('transact_write_items', 'TransactionCanceledException', http_status_code=400)
        with self.assertRaises(SourceGrantConflict):
            self.step()
        self.stub.assert_no_pending_responses()
        self.client.transact_write_items = MagicMock()
        for _ in range(2):
            self.snapshot()
            self.step()
        first, second = (call.kwargs for call in self.client.transact_write_items.call_args_list)
        self.assertEqual(first, second)

    def test_cross_stage_registry_is_rejected_without_calls(self):
        with self.assertRaises(ValueError):
            DynamoSourceGrantDeletion(auth_table='foodtech-auth-sessions-dev',
                catalog_table='foodtech-source-access-dev', registry_table='foodtech-deletion-registry-demo',
                client=self.client, clock=lambda: 1000 + GRACE_SECONDS)
