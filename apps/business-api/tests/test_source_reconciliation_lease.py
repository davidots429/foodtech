import copy
import unittest
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.stub import ANY, Stubber

from foodtech_business_api.source_grant_contract import SourceGrantConflict
from foodtech_business_api.source_reconciliation_lease import SourceReconciliationLeaseStore, SourceGrantRestoreSession
from foodtech_business_api.source_grant_reconciliation import SourceGrantReconciliation


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.request, self.restore = str(uuid4()), str(uuid4())
        self.arn = 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-source-access-restore-' + str(uuid4())
        self.client = boto3.client('dynamodb', region_name='ap-southeast-2',
            aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        self.store = SourceReconciliationLeaseStore(registry_table='foodtech-deletion-registry-dev',
            restored_catalog_arn=self.arn, client=self.client, clock=lambda: 100)
        self.scope = dict(request_id=self.request, reconciliation_id=self.restore, expected_version=4)
        self.params = []
        self.client.meta.events.register('before-parameter-build.dynamodb.UpdateItem',
                                        lambda params, **_: self.params.append(copy.deepcopy(params)))

    def test_acquire_conditions_existing_retained_request_without_replacing_tombstone(self):
        with Stubber(self.client) as stub:
            stub.add_response('update_item', {}, {'TableName': self.store.registry_table, 'Key': ANY,
                'UpdateExpression': ANY, 'ConditionExpression': ANY, 'ExpressionAttributeNames': ANY,
                'ExpressionAttributeValues': ANY, 'ReturnValues': 'NONE', 'ReturnValuesOnConditionCheckFailure': 'NONE'})
            result = self.store.acquire(**self.scope, hold_until=200)
        self.assertEqual(result, {**self.scope, 'expected_version': 5, 'hold_until': 200})
        request = self.params[0]
        for condition in ['request_id = :request', '#version = :expected', '#status = :completed OR #status = :failed',
                          'reconcile_until > :now', 'expires_at > :now', 'reconciliation_hold_until <= :now']:
            self.assertIn(condition, request['ConditionExpression'])
        self.assertNotIn('reconcile_until', request['UpdateExpression'])
        self.assertNotIn('expires_at', request['UpdateExpression'])
        self.assertNotIn('encrypted_user_id', request['UpdateExpression'])
        self.assertNotIn('COMPLETED', request['UpdateExpression'])

    def test_canceled_missing_expired_or_concurrent_request_is_not_unconditionally_retried(self):
        with Stubber(self.client) as stub:
            stub.add_client_error('update_item', service_error_code='ConditionalCheckFailedException')
            with self.assertRaises(SourceGrantConflict):
                self.store.acquire(**self.scope, hold_until=200)
            stub.assert_no_pending_responses()
        self.assertEqual(len(self.params), 1)

    def test_renewal_advances_version_and_requires_exact_previous_active_hold(self):
        with Stubber(self.client) as stub:
            stub.add_response('update_item', {})
            result = self.store.renew(**self.scope, previous_hold_until=200, hold_until=300)
        self.assertEqual(result['expected_version'], 5)
        request = self.params[0]
        for text in ['#status = :active', '#version = :expected', 'reconciliation_id = :restore',
                     'source_grant_restore_table_arn = :table', 'reconciliation_hold_until = :previous',
                     'reconciliation_hold_until > :now']:
            self.assertIn(text, request['ConditionExpression'])
        decoder = TypeDeserializer()
        values = {k: decoder.deserialize(v) for k, v in request['ExpressionAttributeValues'].items()}
        self.assertEqual(values[':previous'], 200)
        self.assertEqual(values[':until'], 300)
        self.assertEqual(values[':table'], self.arn)

    def test_invalid_bounds_and_expired_renewal_do_not_call_sdk(self):
        with Stubber(self.client) as stub:
            for hold in [100, 701, True, '200', None]:
                with self.subTest(hold=hold), self.assertRaises(ValueError):
                    self.store.acquire(**self.scope, hold_until=hold)
            with self.assertRaises(ValueError):
                self.store.acquire(**{**self.scope, 'expected_version': 2**63 - 1}, hold_until=200)
            for previous, until in [(100, 200), (200, 150), (True, 200)]:
                with self.subTest(previous=previous, until=until), self.assertRaises(ValueError):
                    self.store.renew(**self.scope, previous_hold_until=previous, hold_until=until)
            stub.assert_no_pending_responses()


    def test_uncertain_acquire_recovers_only_exact_owned_live_version_without_writes(self):
        record = {'pk': 'REGISTRY', 'sk': 'REQ#' + self.request, 'request_id': self.request,
            'status': 'RECONCILING', 'version': 5, 'reconciliation_id': self.restore,
            'reconciliation_hold_until': 200, 'source_grant_restore_table_arn': self.arn}
        with Stubber(self.client) as stub:
            stub.add_response('get_item', {'Item': {k: self.store.serializer.serialize(v) for k, v in record.items()}})
            result = self.store.recover(**{**self.scope, 'expected_version': 5})
            stub.assert_no_pending_responses()
        self.assertEqual(result['expected_version'], 5)
        self.assertEqual(result['hold_until'], 200)
        self.assertEqual(self.params, [])

    def test_recovery_rejects_canceled_changed_or_expired_lease_without_renewal(self):
        original = {'pk': 'REGISTRY', 'sk': 'REQ#' + self.request, 'request_id': self.request,
            'status': 'RECONCILING', 'version': 5, 'reconciliation_id': self.restore,
            'reconciliation_hold_until': 200, 'source_grant_restore_table_arn': self.arn}
        for changed in [{'status': 'CANCELED'}, {'version': 6}, {'reconciliation_id': str(uuid4())},
                        {'source_archive_purge_id': str(uuid4())},
                        {'reconciliation_hold_until': 100}, {'source_grant_restore_table_arn': self.arn + 'x'}]:
            with self.subTest(changed=changed), Stubber(self.client) as stub:
                row = {**original, **changed}
                stub.add_response('get_item', {'Item': {k: self.store.serializer.serialize(v) for k, v in row.items()}})
                with self.assertRaises(SourceGrantConflict):
                    self.store.recover(**{**self.scope, 'expected_version': 5})
                stub.assert_no_pending_responses()
        self.assertEqual(self.params, [])

    def test_session_connects_acquired_version_to_source_transaction(self):
        grant = str(uuid4())
        subject = str(uuid4())
        def use(**scope):
            self.assertEqual(scope['subject'], subject)
            self.assertEqual(scope['request_id'], self.request)
            scope['action']({'JEYUK#jeyuk-restore': grant})
        inventory = SimpleNamespace(catalog_table='foodtech-source-access-dev',
            cipher=SimpleNamespace(source_table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-source-access-dev'),
            store=SimpleNamespace(registry_table=self.store.registry_table, auth_table='auth', progress_table='progress'),
            use_references=use)
        worker = SourceGrantReconciliation(inventory=inventory, client=self.client,
                                          restored_catalog_arn=self.arn, clock=lambda: 100)
        session = SourceGrantRestoreSession(leases=self.store, reconciliation=worker)
        row = {'pk': 'REGISTRY', 'sk': 'REQ#' + self.request, 'request_id': self.request,
            'status': 'RECONCILING', 'version': 5, 'reconciliation_id': self.restore,
            'reconciliation_hold_until': 200, 'source_grant_restore_table_arn': self.arn}
        encoded = {k: self.store.serializer.serialize(v) for k, v in row.items()}
        writes = []
        self.client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
                                        lambda params, **_: writes.append(copy.deepcopy(params)))
        with Stubber(self.client) as stub:
            stub.add_response('update_item', {})
            stub.add_response('get_item', {'Item': encoded})
            stub.add_response('transact_write_items', {})
            stub.add_response('get_item', {'Item': encoded})
            result = session.start(subject=subject, **self.scope, hold_until=200)
            stub.assert_no_pending_responses()
        self.assertEqual(result['lease']['expected_version'], 5)
        self.assertEqual(result['source_progress']['delete_acknowledged'], 1)
        self.assertFalse(result['restore_promotion_allowed'])
        self.assertFalse(result['source_progress']['deletion_complete'])
        registry = writes[0]['TransactItems'][0]['ConditionCheck']
        self.assertEqual(registry['ExpressionAttributeValues'][':version'], {'N': '5'})
        # No automatic status/hold release follows the domain's observation.
        self.assertEqual(len(self.params), 1)

    def test_failed_source_session_does_not_release_hold_or_declare_completion(self):
        reconciliation = SimpleNamespace(client=self.client, inventory=SimpleNamespace(
            store=SimpleNamespace(registry_table=self.store.registry_table)), restored_arn=self.arn,
            delete_page=Mock(side_effect=SourceGrantConflict('synthetic failure')))
        session = SourceGrantRestoreSession(leases=self.store, reconciliation=reconciliation)
        with Stubber(self.client) as stub:
            stub.add_response('update_item', {})
            with self.assertRaises(SourceGrantConflict):
                session.start(subject=str(uuid4()), **self.scope, hold_until=200)
            stub.assert_no_pending_responses()
        self.assertEqual(len(self.params), 1)
        self.assertEqual(reconciliation.delete_page.call_args.kwargs['expected_version'], 5)


    def retired_session(self, references):
        subject = str(uuid4())

        def use_scope(*, request_id, action):
            self.assertEqual(request_id, self.request)
            action(subject, bytearray(b'archived-head'))

        def use_references(*, subject: str, request_id, action):
            self.assertEqual(subject, archived_subject)
            self.assertEqual(request_id, self.request)
            action(references)

        archived_subject = subject
        inventory = SimpleNamespace(catalog_table='foodtech-source-access-dev',
            cipher=SimpleNamespace(source_table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-source-access-dev'),
            store=SimpleNamespace(registry_table=self.store.registry_table, auth_table='auth', progress_table='progress'),
            use_references=Mock(side_effect=use_references), use_retired_scope=Mock(side_effect=use_scope))
        worker = SourceGrantReconciliation(inventory=inventory, client=self.client,
                                          restored_catalog_arn=self.arn, clock=lambda: 100)
        row = {'pk': 'REGISTRY', 'sk': 'REQ#' + self.request, 'request_id': self.request,
            'status': 'RECONCILING', 'version': 4, 'reconciliation_id': self.restore,
            'reconciliation_hold_until': 200, 'source_grant_restore_table_arn': self.arn}
        lease_read = {'Item': self.store._encode(row)}
        return SourceGrantRestoreSession(leases=self.store, reconciliation=worker), inventory, lease_read

    def test_retired_page_reapplies_and_observes_under_exact_hold(self):
        grant = str(uuid4())
        session, inventory, lease = self.retired_session({'JEYUK#jeyuk-restore': grant})
        writes, reads = [], []
        self.client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
                                        lambda params, **_: writes.append(copy.deepcopy(params)))
        self.client.meta.events.register('before-parameter-build.dynamodb.TransactGetItems',
                                        lambda params, **_: reads.append(copy.deepcopy(params)))
        for observed, expected in (({}, 'CATALOG_EMPTY_OBSERVED'),
                                   ({'Item': {'pk': {'S': 'GRANT#' + grant}}}, 'RESIDUAL_FOUND')):
            with self.subTest(expected=expected), Stubber(self.client) as stub:
                stub.add_response('get_item', lease)
                stub.add_response('get_item', lease)
                stub.add_response('transact_write_items', {})
                stub.add_response('get_item', lease)
                stub.add_response('get_item', lease)
                stub.add_response('transact_get_items', {'Responses': [observed]})
                stub.add_response('get_item', lease)
                result = session.advance_retired(**self.scope, start=0)
                stub.assert_no_pending_responses()
            self.assertEqual(result['source_progress']['next_index'], 1)
            self.assertEqual(result['source_observation']['state'], expected)
            self.assertFalse(result['restore_promotion_allowed'])
            condition, delete = writes[-1]['TransactItems']
            self.assertEqual(condition['ConditionCheck']['ExpressionAttributeValues'][':version'], {'N': '4'})
            self.assertEqual(delete['Delete']['TableName'], self.arn)
            self.assertEqual(reads[-1]['TransactItems'][0]['Get']['TableName'], self.arn)
            self.assertEqual(delete['Delete']['Key'], reads[-1]['TransactItems'][0]['Get']['Key'])
        self.assertEqual(inventory.use_retired_scope.call_count, 2)
        self.assertEqual(self.params, [])

    def test_partial_page_defers_residual_check(self):
        session, _, lease = self.retired_session({'JEYUK#one': str(uuid4()), 'JEYUK#two': str(uuid4())})
        with Stubber(self.client) as stub:
            stub.add_response('get_item', lease)
            stub.add_response('get_item', lease)
            stub.add_response('transact_write_items', {})
            stub.add_response('get_item', lease)
            result = session.advance_retired(**self.scope, start=0, limit=1)
            stub.assert_no_pending_responses()
        self.assertEqual(result['source_progress']['next_index'], 1)
        self.assertIsNone(result['source_observation'])

    def test_missing_lease_skips_archive_and_changed_lease_prevents_deletion(self):
        session, inventory, lease = self.retired_session({'JEYUK#jeyuk-restore': str(uuid4())})
        with Stubber(self.client) as stub:
            stub.add_response('get_item', {})
            with self.assertRaises(SourceGrantConflict):
                session.advance_retired(**self.scope, start=0)
            stub.assert_no_pending_responses()
        inventory.use_retired_scope.assert_not_called()
        with Stubber(self.client) as stub:
            stub.add_response('get_item', lease)
            stub.add_response('get_item', lease)
            stub.add_client_error('transact_write_items', service_error_code='TransactionCanceledException')
            with self.assertRaises(SourceGrantConflict):
                session.advance_retired(**self.scope, start=0)
            stub.assert_no_pending_responses()
        self.assertEqual(self.params, [])


if __name__ == '__main__':
    unittest.main()
