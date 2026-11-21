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
        store.serializer = TypeSerializer()
        store.append_meal_live('meal', 5, {'sk':'META', 'count':10}, {'sk':'CHUNK#000005', 'records':['x']})
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(ops), 3)
        self.assertIn('attribute_not_exists(#source)', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[1]['Put']['ConditionExpression'], '#count = :expected')
        self.assertEqual(ops[1]['Put']['ExpressionAttributeValues'][':expected'], {'N':'5'})
        self.assertEqual(ops[2]['Put']['ConditionExpression'], 'attribute_not_exists(pk)')

    def test_final_attachment_guards_live_cursor(self):
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='table'); store.client = Mock(); store.serializer = TypeSerializer()
        store.attach_meal_source('meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], 10)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[2]['Put']['Item']['sk'], {'S': 'MEAL_AGGREGATE#DIRTY'})
        self.assertEqual(ops[2]['Put']['Item']['version'], {'N': '2'})
        self.assertRegex(ops[2]['Put']['Item']['aggregate_refresh_shard']['S'], r'^PENDING#0[0-9a-f]$')
        self.assertEqual(ops[3]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N':'10'})
        store.attach_meal_source('meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], None)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[3]['ConditionCheck']['ConditionExpression'], 'attribute_not_exists(pk)')

    def test_dynamo_transaction_conflict_is_reported_as_conflict(self):
        class Canceled(Exception):
            def __init__(self, codes):
                self.response = {"CancellationReasons": [{"Code": code} for code in codes]}
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="table"); store.client = Mock(); store.serializer = TypeSerializer()
        store.client.exceptions.TransactionCanceledException = Canceled
        store.client.transact_write_items.side_effect = Canceled(["None", "TransactionConflict", "None"])
        with self.assertRaises(Conflict):
            store.append_meal_live("meal", 0, {"sk": "META", "count": 1}, {"sk": "CHUNK#000000"})
        store.client.transact_write_items.side_effect = Canceled(["TransactionConflict", "ProvisionedThroughputExceeded"])
        with self.assertRaises(Canceled):
            store.append_meal_live("meal", 0, {"sk": "META", "count": 1}, {"sk": "CHUNK#000000"})

    def test_memory_finalization_detects_competing_append(self):
        store = MemoryCookingStore()
        store.create_meal('meal', [{'sk':'META','version':1}])
        store.append_meal_live('meal', 0, {'sk':'META','count':5}, {'sk':'CHUNK#000000'})
        with self.assertRaises(Conflict):
            store.attach_meal_source('meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], None)
        self.assertIsNone(store.read('meal')[0].get('source'))
        self.assertNotIn(('meal', 'MEAL_AGGREGATE#DIRTY'), store.items)

    def test_successful_revision_records_pending_version_and_conflict_keeps_it(self):
        store = MemoryCookingStore()
        store.create_meal('meal', [{'sk': 'META', 'version': 1}, {'sk': 'JOB', 'status': 'PENDING'}])
        store.advance_meal('meal', 1, {'sk': 'META', 'version': 2},
                           {'sk': 'JOB', 'status': 'DONE'}, {'sk': 'EVENT#2'})
        marker = store.items[('meal', 'MEAL_AGGREGATE#DIRTY')]
        self.assertEqual((marker['state'], marker['version']), ('PENDING', 2))
        shard = marker['aggregate_refresh_shard']
        self.assertRegex(shard, r'^PENDING#0[0-9a-f]$')
        with self.assertRaises(Conflict):
            store.advance_meal('meal', 1, {'sk': 'META', 'version': 2},
                               {'sk': 'JOB', 'status': 'DONE'}, {'sk': 'EVENT#3'})
        self.assertEqual(store.items[('meal', 'MEAL_AGGREGATE#DIRTY')]['version'], 2)
        self.assertEqual(store.items[('meal', 'MEAL_AGGREGATE#DIRTY')]['aggregate_refresh_shard'], shard)
