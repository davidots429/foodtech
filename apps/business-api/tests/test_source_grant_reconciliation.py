import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

from foodtech_business_api.dynamodb_restore_verification import DynamoRestoreVerificationError
from foodtech_business_api.source_grant_contract import SourceGrantConflict
from foodtech_business_api.source_grant_reconciliation import SourceGrantReconciliation


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.request, self.restore = str(uuid4()), str(uuid4())
        self.subject = str(uuid4())
        self.client = boto3.client('dynamodb', region_name='ap-southeast-2',
            aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        self.serializer = TypeSerializer()
        self.references = {'JEYUK#jeyuk-' + str(i): str(uuid4()) for i in range(101)}
        def use(**scope):
            self.assertEqual((scope['subject'], scope['request_id']), (self.subject, self.request))
            scope['action'](dict(self.references))
            return {'references_used': len(self.references), 'deletion_complete': False}
        self.inventory = SimpleNamespace(catalog_table='foodtech-source-access-dev',
            cipher=SimpleNamespace(source_table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-source-access-dev'),
            store=SimpleNamespace(auth_table='foodtech-auth-sessions-dev', registry_table='foodtech-deletion-registry-dev',
                                  progress_table='foodtech-deletion-progress-dev'), use_references=use)
        self.arn = 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-source-access-restore-' + str(uuid4())
        self.worker = SourceGrantReconciliation(inventory=self.inventory, client=self.client,
                                               restored_catalog_arn=self.arn, clock=lambda: 100)
        self.scope = dict(subject=self.subject, request_id=self.request, reconciliation_id=self.restore, expected_version=4)
        self.record = {'pk': 'REGISTRY', 'sk': 'REQ#' + self.request, 'request_id': self.request,
            'status': 'RECONCILING', 'version': 4, 'reconciliation_id': self.restore,
            'reconciliation_hold_until': 200, 'source_grant_restore_table_arn': self.arn}

    def lease(self, stub, changed=None):
        row = {**self.record, **(changed or {})}
        stub.add_response('get_item', {'Item': {k: self.serializer.serialize(v) for k, v in row.items()}},
            {'TableName': self.inventory.store.registry_table, 'Key': ANY, 'ConsistentRead': True,
             'ProjectionExpression': ANY, 'ExpressionAttributeNames': ANY})

    def test_delete_checks_live_registry_and_never_targets_live_catalog(self):
        writes = []
        self.client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
                                        lambda params, **_: writes.append(copy.deepcopy(params)))
        with Stubber(self.client) as stub:
            self.lease(stub)
            stub.add_response('transact_write_items', {}, {'TransactItems': ANY, 'ClientRequestToken': ANY})
            stub.add_response('transact_write_items', {}, {'TransactItems': ANY, 'ClientRequestToken': ANY})
            self.lease(stub)
            result = self.worker.delete_page(**self.scope, limit=2)
            stub.assert_no_pending_responses()
        self.assertEqual(result, {'delete_acknowledged': 2, 'next_index': 2,
                                 'references_exhausted': False, 'deletion_complete': False})
        for write in writes:
            registry, delete = write['TransactItems']
            self.assertEqual(registry['ConditionCheck']['TableName'], self.inventory.store.registry_table)
            expression = registry['ConditionCheck']['ConditionExpression']
            for text in ['#status = :active', '#version = :version', 'reconciliation_id = :restore',
                         'reconciliation_hold_until > :now', 'source_grant_restore_table_arn = :table']:
                self.assertIn(text, expression)
            self.assertEqual(delete['Delete']['TableName'], self.worker.table)
            self.assertNotEqual(delete['Delete']['TableName'], self.inventory.catalog_table)
            self.assertIn('grant_id = :grant AND source_partition = :source', delete['Delete']['ConditionExpression'])
            self.assertTrue(all(next(iter(op.values()))['ReturnValuesOnConditionCheckFailure'] == 'NONE'
                                for op in write['TransactItems']))

    def test_canceled_expired_missing_or_changed_lease_never_deletes(self):
        for changed in [{'status': 'CANCELED'}, {'status': 'COMPLETED'}, {'version': 5},
                        {'source_archive_purge_id': str(uuid4())},
                        {'reconciliation_id': str(uuid4())}, {'reconciliation_hold_until': 100},
                        {'source_grant_restore_table_arn': self.inventory.cipher.source_table_arn}]:
            with self.subTest(changed=changed), Stubber(self.client) as stub:
                self.lease(stub, changed)
                with self.assertRaises(SourceGrantConflict):
                    self.worker.delete_page(**self.scope, limit=1)
                stub.assert_no_pending_responses()
        with Stubber(self.client) as stub:
            stub.add_response('get_item', {})
            with self.assertRaises(SourceGrantConflict):
                self.worker.delete_page(**self.scope)

    def test_cancellation_race_is_rejected_in_delete_transaction(self):
        with Stubber(self.client) as stub:
            self.lease(stub)
            stub.add_client_error('transact_write_items', service_error_code='TransactionCanceledException',
                expected_params={'TransactItems': ANY, 'ClientRequestToken': ANY})
            with self.assertRaises(SourceGrantConflict):
                self.worker.delete_page(**self.scope, limit=2)
            stub.assert_no_pending_responses()

    def test_hold_expiring_after_archive_read_stops_before_mutation(self):
        ticks = iter([100, 200])
        self.worker.clock = lambda: next(ticks)
        with Stubber(self.client) as stub:
            self.lease(stub)
            with self.assertRaises(SourceGrantConflict):
                self.worker.delete_page(**self.scope, limit=1)
            stub.assert_no_pending_responses()

    def test_101_reference_residual_observation_never_promotes_restore(self):
        for present in [False, True]:
            with self.subTest(present=present), Stubber(self.client) as stub:
                self.lease(stub)
                first = [{}] * 100
                if present:
                    first[0] = {'Item': {'pk': {'S': 'GRANT#residual'}, 'sk': {'S': 'META'}}}
                stub.add_response('transact_get_items', {'Responses': first})
                stub.add_response('transact_get_items', {'Responses': [{}]})
                self.lease(stub)
                result = self.worker.observe_empty(**self.scope)
                self.assertEqual(result, {'state': 'RESIDUAL_FOUND' if present else 'CATALOG_EMPTY_OBSERVED',
                                         'deletion_complete': False, 'restore_promotion_allowed': False})
                stub.assert_no_pending_responses()

    def test_changed_lease_after_empty_observation_does_not_return_success(self):
        with Stubber(self.client) as stub:
            self.lease(stub)
            stub.add_response('transact_get_items', {'Responses': [{}] * 100})
            stub.add_response('transact_get_items', {'Responses': [{}]})
            self.lease(stub, {'version': 5})
            with self.assertRaises(SourceGrantConflict):
                self.worker.observe_empty(**self.scope)

    def test_live_foreign_or_noncanonical_target_configuration_is_rejected(self):
        for arn in [self.inventory.cipher.source_table_arn, self.arn.replace('123456789012', '999999999999'),
                    self.arn.replace('ap-southeast-2', 'us-east-1'), self.arn[:-1] + 'z']:
            with self.subTest(arn=arn), self.assertRaises(ValueError):
                SourceGrantReconciliation(inventory=self.inventory, client=self.client,
                                         restored_catalog_arn=arn, clock=lambda: 100)


    def configure_restored_auth(self):
        self.restore_at = datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)
        self.auth = self.arn.replace('foodtech-source-access-restore-', 'foodtech-auth-sessions-restore-')
        self.worker = SourceGrantReconciliation(inventory=self.inventory, client=self.client,
            restored_catalog_arn=self.arn, restored_auth_arn=self.auth, restore_at=self.restore_at, clock=lambda: 100)

    def restore_pair(self, stub, changed_auth=None):
        for restored, source in (
            (self.arn, self.inventory.cipher.source_table_arn),
            (self.auth, self.arn.split(':table/', 1)[0] + ':table/' + self.inventory.store.auth_table),
        ):
            changes = changed_auth if restored == self.auth else None
            row = {'TableArn': restored, 'TableStatus': 'ACTIVE', 'RestoreSummary': {
                'SourceTableArn': source, 'RestoreInProgress': False, 'RestoreDateTime': self.restore_at}}
            if changes is not None:
                row.update(changes)
            stub.add_response('describe_table', {'Table': row}, {'TableName': restored})

    def auth_response(self, stub, references=None):
        item = {'pk': 'USER#' + self.subject}
        if references is not None:
            item['source_read_grants'] = references
        stub.add_response('get_item', {'Item': {k: self.serializer.serialize(v) for k, v in item.items()}},
            {'TableName': self.auth, 'Key': {'pk': {'S': item['pk']}}, 'ConsistentRead': True,
             'ProjectionExpression': 'pk, #grants', 'ExpressionAttributeNames': {'#grants': 'source_read_grants'}})

    def test_restored_auth_replaced_reference_and_archive_are_both_reapplied_and_verified(self):
        self.configure_restored_auth()
        source = 'JEYUK#jeyuk-example'
        current, older = str(uuid4()), str(uuid4())
        self.references = {source: current}
        restored = {source: older}
        writes, reads = [], []
        for operation, calls in (('TransactWriteItems', writes), ('TransactGetItems', reads)):
            self.client.meta.events.register('before-parameter-build.dynamodb.' + operation,
                lambda params, calls=calls, **_: calls.append(copy.deepcopy(params)))
        with Stubber(self.client) as stub:
            self.lease(stub)
            self.restore_pair(stub)
            self.auth_response(stub, restored)
            stub.add_response('transact_write_items', {})
            stub.add_response('transact_write_items', {})
            self.lease(stub)
            result = self.worker.delete_page(**self.scope)
            self.lease(stub)
            self.restore_pair(stub)
            self.auth_response(stub, restored)
            stub.add_response('transact_get_items', {'Responses': [{}, {}]})
            self.auth_response(stub, restored)
            self.lease(stub)
            observation = self.worker.observe_empty(**self.scope)
            stub.assert_no_pending_responses()
        self.assertEqual(result['delete_acknowledged'], 2)
        self.assertTrue(observation['restored_auth_checked'])
        self.assertFalse(observation['restore_promotion_allowed'])
        deleted = set()
        for write in writes:
            registry, delete, auth = write['TransactItems']
            self.assertEqual(registry['ConditionCheck']['TableName'], self.inventory.store.registry_table)
            self.assertEqual(auth['ConditionCheck']['TableName'], self.auth)
            self.assertIn('#grants = :grants', auth['ConditionCheck']['ConditionExpression'])
            self.assertEqual(auth['ConditionCheck']['ExpressionAttributeValues'][':grants'],
                             self.serializer.serialize(restored))
            deleted.add(delete['Delete']['Key']['pk']['S'])
        self.assertEqual(deleted, {'GRANT#' + current, 'GRANT#' + older})
        self.assertEqual({op['Get']['Key']['pk']['S'] for op in reads[0]['TransactItems']}, deleted)

    def test_restored_auth_drift_or_conflicting_identifier_blocks_evidence(self):
        self.configure_restored_auth()
        source = 'JEYUK#jeyuk-example'
        grant = str(uuid4())
        self.references = {source: grant}
        with Stubber(self.client) as stub:
            self.lease(stub)
            self.restore_pair(stub)
            self.auth_response(stub, {'JEYUK#jeyuk-other': grant})
            with self.assertRaisesRegex(SourceGrantConflict, 'conflicting bindings'):
                self.worker.delete_page(**self.scope)
            stub.assert_no_pending_responses()
        with Stubber(self.client) as stub:
            self.lease(stub)
            self.restore_pair(stub)
            self.auth_response(stub, {source: grant})
            stub.add_response('transact_get_items', {'Responses': [{}]})
            self.auth_response(stub, {})
            with self.assertRaisesRegex(SourceGrantConflict, 'account changed'):
                self.worker.observe_empty(**self.scope)
            stub.assert_no_pending_responses()

    def test_other_restore_auth_table_is_rejected(self):
        self.configure_restored_auth()
        for arn in (self.auth.replace('123456789012', '123456789013'),
                    self.auth[:self.auth.index('-restore-')] + '-dev',
                    self.auth.rsplit('-restore-', 1)[0] + '-restore-' + self.restore):
            with self.subTest(arn=arn), self.assertRaises(ValueError):
                SourceGrantReconciliation(inventory=self.inventory, client=self.client,
                    restored_catalog_arn=self.arn, restored_auth_arn=arn, clock=lambda: 100)


    def test_wrong_restore_timestamp_source_or_incomplete_metadata_stops_before_data_reads(self):
        self.configure_restored_auth()
        for summary in (
            {'SourceTableArn': self.inventory.cipher.source_table_arn, 'RestoreInProgress': False,
             'RestoreDateTime': self.restore_at},
            {'SourceTableArn': self.worker.source_auth_arn, 'RestoreInProgress': False,
             'RestoreDateTime': self.restore_at + timedelta(seconds=1)},
            {'SourceTableArn': self.worker.source_auth_arn, 'RestoreInProgress': True,
             'RestoreDateTime': self.restore_at},
        ):
            with self.subTest(summary=summary), Stubber(self.client) as stub:
                self.lease(stub)
                self.restore_pair(stub, {'RestoreSummary': summary})
                with self.assertRaises(DynamoRestoreVerificationError):
                    self.worker.delete_page(**self.scope)
                stub.assert_no_pending_responses()

    def test_unqualified_restore_time_is_rejected_before_access(self):
        self.configure_restored_auth()
        for value in (None, self.restore_at.replace(tzinfo=None), '2026-11-28T10:00Z'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'restore point'):
                SourceGrantReconciliation(inventory=self.inventory, client=self.client,
                    restored_catalog_arn=self.arn, restored_auth_arn=self.auth, restore_at=value, clock=lambda: 100)


if __name__ == '__main__':
    unittest.main()
