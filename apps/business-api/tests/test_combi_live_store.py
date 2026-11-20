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
        self.assertEqual(ops[2]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N':'10'})
        store.attach_meal_source('meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], None)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[2]['ConditionCheck']['ConditionExpression'], 'attribute_not_exists(pk)')

    def test_memory_finalization_detects_competing_append(self):
        store = MemoryCookingStore()
        store.create_meal('meal', [{'sk':'META','version':1}])
        store.append_meal_live('meal', 0, {'sk':'META','count':5}, {'sk':'CHUNK#000000'})
        with self.assertRaises(Conflict):
            store.attach_meal_source('meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], None)
        self.assertIsNone(store.read('meal')[0].get('source'))
