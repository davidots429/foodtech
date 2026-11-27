"""Live append and final attachment must use conditional, atomic DynamoDB writes."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.cooking_store import DynamoCookingStore, MemoryCookingStore, Conflict

class CombiLiveStoreTests(unittest.TestCase):
    def test_dynamo_append_guards_meal_cursor_and_immutable_chunk(self):
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='table')
        store.client = Mock()
        store.serializer = TypeSerializer(); store.auth_table_name = 'auth'
        store.append_meal_live('SCOPE#USER#owner#COMBI_MEAL#meal', 5, {'sk':'META', 'count':10}, {'sk':'CHUNK#000005', 'records':['x']})
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(ops), 4)
        self.assertIn('attribute_exists(pk)', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertIn('deletion_lock_request', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertIn('attribute_not_exists(#source)', ops[1]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[2]['Put']['ConditionExpression'], '#count = :expected')
        self.assertEqual(ops[2]['Put']['ExpressionAttributeValues'][':expected'], {'N':'5'})
        self.assertEqual(ops[3]['Put']['ConditionExpression'], 'attribute_not_exists(pk)')

    def test_final_attachment_guards_live_cursor(self):
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='table'); store.client = Mock(); store.serializer = TypeSerializer(); store.auth_table_name = 'auth'
        store.attach_meal_source('SCOPE#USER#owner#COMBI_MEAL#meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], 10)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertIn('attribute_exists(pk)', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[3]['Put']['Item']['sk'], {'S': 'MEAL_AGGREGATE#DIRTY'})
        self.assertEqual(ops[3]['Put']['Item']['version'], {'N': '2'})
        self.assertRegex(ops[3]['Put']['Item']['aggregate_refresh_shard']['S'], r'^PENDING#0[0-9a-f]$')
        self.assertEqual(ops[4]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N':'10'})
        store.attach_meal_source('SCOPE#USER#owner#COMBI_MEAL#meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], None)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[4]['ConditionCheck']['ConditionExpression'], 'attribute_not_exists(pk)')

    def test_creation_and_step_use_owner_epoch_guard(self):
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='table'); store.client = Mock(); store.serializer = TypeSerializer(); store.auth_table_name = 'auth'
        pk = 'SCOPE#USER#owner#COMBI_MEAL#meal'
        store.create_meal(pk, [{'sk': 'META', 'version': 1, 'meal_batch_id': 'meal'}], 4)
        guard = store.client.transact_write_items.call_args.kwargs['TransactItems'][0]['ConditionCheck']
        self.assertEqual(guard['Key']['pk'], {'S': 'USER#owner'})
        self.assertEqual(guard['ExpressionAttributeValues'][':expected'], {'N': '4'})
        store.advance_meal(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'STEP#rice'}, {'sk': 'EVENT#1'}, 4)
        guard = store.client.transact_write_items.call_args.kwargs['TransactItems'][0]['ConditionCheck']
        self.assertIn('deletion_lock_request', guard['ConditionExpression'])
        self.assertIn('attribute_exists(pk)', guard['ConditionExpression'])
        self.assertEqual(store.client.transact_write_items.call_args.kwargs['TransactItems'][4]['Put']['Item']['sk'], {'S': 'MEAL_AGGREGATE#DIRTY'})
        with self.assertRaises(ValueError):
            store.create_meal('invalid', [{'sk': 'META'}], 4)

    def test_dynamo_transaction_conflict_is_reported_as_conflict(self):
        class Canceled(Exception):
            def __init__(self, codes):
                self.response = {"CancellationReasons": [{"Code": code} for code in codes]}
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="table"); store.client = Mock(); store.serializer = TypeSerializer(); store.auth_table_name = 'auth'
        store.client.exceptions.TransactionCanceledException = Canceled
        store.client.transact_write_items.side_effect = Canceled(["None", "TransactionConflict", "None"])
        with self.assertRaises(Conflict):
            store.append_meal_live("SCOPE#USER#owner#COMBI_MEAL#meal", 0, {"sk": "META", "count": 1}, {"sk": "CHUNK#000000"})
        store.client.transact_write_items.side_effect = Canceled(["TransactionConflict", "ProvisionedThroughputExceeded"])
        with self.assertRaises(Canceled):
            store.append_meal_live("SCOPE#USER#owner#COMBI_MEAL#meal", 0, {"sk": "META", "count": 1}, {"sk": "CHUNK#000000"})

    def test_memory_finalization_detects_competing_append(self):
        store = MemoryCookingStore()
        store.create_meal('SCOPE#USER#owner#COMBI_MEAL#meal', [{'sk':'META','version':1,'meal_batch_id':'meal'}])
        store.append_meal_live('SCOPE#USER#owner#COMBI_MEAL#meal', 0, {'sk':'META','count':5}, {'sk':'CHUNK#000000'})
        with self.assertRaises(Conflict):
            store.attach_meal_source('SCOPE#USER#owner#COMBI_MEAL#meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], None)
        self.assertIsNone(store.read('SCOPE#USER#owner#COMBI_MEAL#meal')[0].get('source'))

    def test_successful_revision_records_pending_version_and_conflict_keeps_it(self):
        store = MemoryCookingStore()
        pk = 'SCOPE#USER#owner#COMBI_MEAL#meal'
        store.create_meal(pk, [{'sk': 'META', 'version': 1, 'meal_batch_id': 'meal'}, {'sk': 'JOB', 'status': 'PENDING'}])
        store.advance_meal(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'JOB', 'status': 'DONE'}, {'sk': 'EVENT#2'})
        marker = store.items[(pk, 'MEAL_AGGREGATE#DIRTY')]
        self.assertEqual((marker['state'], marker['version']), ('PENDING', 2))
        shard = marker['aggregate_refresh_shard']
        self.assertRegex(shard, r'^PENDING#0[0-9a-f]$')
        with self.assertRaises(Conflict):
            store.advance_meal(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'JOB', 'status': 'DONE'}, {'sk': 'EVENT#3'})
        self.assertEqual(store.items[(pk, 'MEAL_AGGREGATE#DIRTY')]['version'], 2)
        self.assertEqual(store.items[(pk, 'MEAL_AGGREGATE#DIRTY')]['aggregate_refresh_shard'], shard)

    def test_meal_inventory_is_owner_scoped_paginated_and_atomic(self):
        store = MemoryCookingStore()
        for owner, meal in [('alice', 'one'), ('alice', 'two'), ('bob', 'private')]:
            store.create_meal(f'SCOPE#USER#{owner}#COMBI_MEAL#{meal}',
                              [{'sk': 'META', 'meal_batch_id': meal}])
        first = store.list_meal_refs('alice', 1, None)
        second = store.list_meal_refs('alice', 1, first['next_cursor'])
        self.assertEqual(first['items'] + second['items'], [{'meal_batch_id': 'one'}, {'meal_batch_id': 'two'}])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(store.list_meal_refs('bob', 5, None)['items'], [{'meal_batch_id': 'private'}])
        with self.assertRaises(Conflict):
            store.create_meal('SCOPE#USER#alice#COMBI_MEAL#one', [{'sk': 'META', 'meal_batch_id': 'one'}])
        with self.assertRaises(ValueError):
            store.create_meal('SCOPE#USER#alice#COMBI_MEAL#other', [{'sk': 'META', 'meal_batch_id': 'one'}])
        with self.assertRaises(ValueError):
            store.list_meal_refs('alice', 1, 'MEAL#bad#cursor')

    def test_legacy_meal_reconciliation_requires_matching_source(self):
        store = MemoryCookingStore()
        source = ('SCOPE#USER#alice#COMBI_MEAL#old', 'META')
        store.items[source] = {'meal_batch_id': 'old'}
        store.ensure_meal_listed('alice', 'old')
        store.ensure_meal_listed('alice', 'old')
        self.assertEqual(store.list_meal_refs('alice', 10, None)['items'], [{'meal_batch_id': 'old'}])
        with self.assertRaises(Conflict):
            store.ensure_meal_listed('bob', 'old')
        store.items[source]['meal_batch_id'] = 'changed'
        with self.assertRaises(Conflict):
            store.ensure_meal_listed('alice', 'old')

    def test_dynamo_reconciliation_guards_auth_and_source(self):
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='table'); store.client = Mock(); store.serializer = TypeSerializer(); store.auth_table_name = 'auth'
        store.ensure_meal_listed('alice', 'old')
        operations = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(operations[0]['ConditionCheck']['TableName'], 'auth')
        self.assertIn('deletion_lock_request', operations[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(operations[1]['ConditionCheck']['Key']['pk'], {'S': 'SCOPE#USER#alice#COMBI_MEAL#old'})
        self.assertIn('meal_batch_id = :id', operations[1]['ConditionCheck']['ConditionExpression'])
        self.assertIn('Put', operations[2])
        self.assertEqual(operations[3]['Update']['Key']['sk'], {'S': 'HISTORY#REVISION'})
