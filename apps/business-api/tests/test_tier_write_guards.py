import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.tier_store import DynamoTierStore, MemoryTierStore


class TierGuardTests(unittest.TestCase):
    def setUp(self):
        self.store = DynamoTierStore.__new__(DynamoTierStore)
        self.store.table = SimpleNamespace(name='tier')
        self.store.client = Mock()
        self.store.client.exceptions.TransactionCanceledException = type('Canceled', (Exception,), {})
        self.store.serializer = TypeSerializer()
        self.store.auth_table_name = 'auth'
        self.item = {'application_id': 'app', 'submitted_at': '2026-01-01',
                     'status': 'REQUESTED', 'version': 1}

    def test_submit_checks_epoch_and_lock(self):
        self.store.submit('customer', self.item, 4)
        ops = self.store.client.transact_write_items.call_args.kwargs['TransactItems']
        guard = ops[0]['ConditionCheck']
        self.assertEqual(guard['Key']['pk'], {'S': 'USER#customer'})
        self.assertEqual(guard['ExpressionAttributeValues'][':expected'], {'N': '4'})
        self.assertIn('deletion_lock_request', guard['ConditionExpression'])
        self.assertEqual(len(ops), 3)

    def test_review_checks_customer_and_admin_without_duplicate_keys(self):
        self.store.current = Mock(return_value=self.item)
        self.store.decide('customer', 'app', 'admin', 'REJECTED', 'now', 5)
        ops = self.store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual([op['ConditionCheck']['Key']['pk']['S'] for op in ops[:2]],
                         ['USER#customer', 'USER#admin'])
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N': '5'})
        self.assertIn('admin_blocked', ops[1]['ConditionCheck']['ConditionExpression'])
        self.store.decide('customer', 'app', 'customer', 'REJECTED', 'now', 5)
        ops = self.store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(ops), 3)
        self.assertEqual(ops[0]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N': '5'})
        self.assertIn('admin_blocked', ops[0]['ConditionCheck']['ConditionExpression'])

    def test_missing_auth_config_fails_closed(self):
        self.store.auth_table_name = ''
        with self.assertRaises(ValueError):
            self.store.submit('customer', self.item)

    def test_inventory_is_scoped_key_only_and_paginated(self):
        store = MemoryTierStore()
        store.submit('alice', {**self.item, 'business_name': 'private'})
        store.decide('alice', 'app', 'admin', 'REJECTED', 'now')
        store.submit('bob', self.item)
        first = store.inventory_keys('alice', 2, None)
        second = store.inventory_keys('alice', 2, first['next_cursor'])
        self.assertEqual(first['keys'] + second['keys'], ['APPLICATION#app', 'CURRENT', 'REVIEW#app'])
        self.assertIsNone(second['next_cursor'])
        self.assertNotIn('private', repr(first))
        with self.assertRaises(ValueError):
            store.inventory_keys('alice#bob', 2, None)

    def test_dynamo_inventory_uses_consistent_tenant_partition(self):
        self.store.table = Mock()
        self.store.table.query.return_value = {
            'Items': [{'pk': 'TENANT#alice', 'sk': 'CURRENT'}],
            'LastEvaluatedKey': {'pk': 'TENANT#alice', 'sk': 'CURRENT'}}
        result = self.store.inventory_keys('alice', 1, 'APPLICATION#old')
        self.assertEqual(result, {'keys': ['CURRENT'], 'next_cursor': 'CURRENT'})
        args = self.store.table.query.call_args.kwargs
        self.assertTrue(args['ConsistentRead'])
        self.assertEqual(args['ProjectionExpression'], 'pk, sk')
        self.assertEqual(args['ExclusiveStartKey'], {'pk': 'TENANT#alice', 'sk': 'APPLICATION#old'})
        self.store.table.query.return_value['Items'][0]['pk'] = 'TENANT#bob'
        with self.assertRaises(ValueError):
            self.store.inventory_keys('alice', 1, None)

    def test_locked_application_candidates_are_bounded_and_owner_checked(self):
        self.store.table = Mock()
        self.store.table.name = 'tier'
        request = str(uuid4())
        self.store.table.query.return_value = {'Items': [{'pk': 'TENANT#alice', 'sk': 'APPLICATION#a',
            'application_id': 'a', 'submitted_at': '2026-01-01T00:00:00Z'}],
            'LastEvaluatedKey': {'pk': 'TENANT#alice', 'sk': 'APPLICATION#a'}}
        result = self.store.deletion_index_candidates('alice', request, 1)
        self.assertEqual(result, {'queue_sorts': ['2026-01-01T00:00:00Z#a'], 'next_cursor': 'APPLICATION#a'})
        args = self.store.table.query.call_args.kwargs
        self.assertTrue(args['ConsistentRead'])
        self.assertEqual(args['Limit'], 1)
        guard = self.store.client.transact_write_items.call_args.kwargs['TransactItems'][0]['ConditionCheck']
        self.assertEqual(guard['ExpressionAttributeValues'][':request'], {'S': request})
        self.store.table.query.return_value['Items'][0]['pk'] = 'TENANT#bob'
        with self.assertRaises(ValueError):
            self.store.deletion_index_candidates('alice', request, 1)
        with self.assertRaises(ValueError):
            self.store.deletion_index_candidates('alice', request, 1, 'OTHER#x')

    def test_index_observation_checks_owner_and_does_not_claim_completion(self):
        self.store.table = Mock()
        self.store.table.name = 'tier'
        request, sort = str(uuid4()), 'date#application'
        other = {'pk': 'TENANT#bob', 'sk': 'CURRENT', 'queue_status': 'REQUESTED', 'queue_sort': sort}
        own = {**other, 'pk': 'TENANT#alice'}
        cursor = {'pk': 'TENANT#bob', 'sk': 'CURRENT', 'queue_status': 'REQUESTED', 'queue_sort': sort}
        self.store.table.query.side_effect = [{'Items': [other], 'LastEvaluatedKey': cursor}, {'Items': [own]}]
        self.assertEqual(self.store.observe_pending_index('alice', request, sort), 'PRESENT')
        self.assertEqual(self.store.table.query.call_args.kwargs['ExclusiveStartKey'], cursor)
        self.store.table.query.side_effect = None
        self.store.table.query.return_value = {'Items': [other]}
        self.assertEqual(self.store.observe_pending_index('alice', request, sort), 'NOT_OBSERVED')
        self.store.table.query.return_value = {'Items': [other], 'LastEvaluatedKey': cursor}
        self.assertEqual(self.store.observe_pending_index('alice', request, sort), 'INCONCLUSIVE')
        self.store.table.query.return_value = {'Items': [{**other, 'queue_sort': 'wrong'}]}
        with self.assertRaises(ValueError):
            self.store.observe_pending_index('alice', request, sort)

    def test_delete_requires_exact_lock_and_targets_only_scoped_key(self):
        request = str(uuid4())
        self.store.delete_inventory_key('alice', request, 'CURRENT')
        operations = self.store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(operations[0]['ConditionCheck']['ExpressionAttributeValues'][':request'], {'S': request})
        self.assertEqual(operations[1]['Delete']['Key'], {'pk': {'S': 'TENANT#alice'}, 'sk': {'S': 'CURRENT'}})
        for subject, request_id, key in [('alice', 'bad', 'CURRENT'), ('alice#bad', request, 'CURRENT'),
                                         ('alice', request, None), ('alice', request, '')]:
            with self.subTest(subject=subject, key=key), self.assertRaises(ValueError):
                self.store.delete_inventory_key(subject, request_id, key)

    def test_page_retries_from_first_page_and_propagates_partial_failure(self):
        request = str(uuid4())
        self.store.inventory_keys = Mock(return_value={'keys': ['a', 'b'], 'next_cursor': 'b'})
        self.store.delete_inventory_key = Mock(side_effect=[None, RuntimeError('failed')])
        with self.assertRaises(RuntimeError):
            self.store.delete_inventory_page('alice', request, 2)
        self.store.inventory_keys.assert_called_once_with('alice', 2, None)
        self.assertEqual(self.store.delete_inventory_key.call_count, 2)
        self.store.delete_inventory_key = Mock()
        self.store.inventory_keys.return_value = {'keys': ['b'], 'next_cursor': None}
        self.assertEqual(self.store.delete_inventory_page('alice', request, 2), 1)
        self.assertEqual(self.store.inventory_keys.call_args.args, ('alice', 2, None))
        self.store.delete_inventory_key.assert_called_once_with('alice', request, 'b')

    def test_pending_excludes_stale_deleted_and_locked_index_entries(self):
        self.store.table, self.store.auth_table = Mock(), Mock()
        def indexed(subject, app):
            return {'pk': f'TENANT#{subject}', 'sk': 'CURRENT', 'subject': subject,
                    'application_id': app, 'status': 'REQUESTED'}
        rows = [indexed('deleted', 'old'), indexed('changed', 'old'), indexed('locked', 'a'),
                indexed('ok', 'a')]
        self.store.table.query.side_effect = [
            {'Items': rows[:2], 'LastEvaluatedKey': {'pk': 'TENANT#changed', 'sk': 'CURRENT'}},
            {'Items': rows[2:]}]
        def current(*, Key, ConsistentRead):
            self.assertTrue(ConsistentRead)
            who = Key['pk'].removeprefix('TENANT#')
            if who == 'deleted': return {}
            return {'Item': indexed(who, 'new' if who == 'changed' else 'a')}
        self.store.table.get_item.side_effect = current
        self.store.auth_table.get_item.side_effect = lambda **kw: {
            'Item': {'deletion_lock_request': 'locked'}} if kw['Key']['pk'] == 'USER#locked' else {}
        result = self.store.pending(2)
        self.assertEqual([item['subject'] for item in result], ['ok'])
        self.assertEqual(self.store.table.query.call_count, 2)
        self.assertEqual(self.store.auth_table.get_item.call_count, 2)

    def test_pending_fails_closed_without_auth_table(self):
        self.store.auth_table = None
        with self.assertRaises(ValueError):
            self.store.pending()


if __name__ == '__main__':
    unittest.main()
