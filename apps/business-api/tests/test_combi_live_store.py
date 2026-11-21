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
        self.assertEqual(ops[3]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N':'10'})
        store.attach_meal_source('SCOPE#USER#owner#COMBI_MEAL#meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], None)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[3]['ConditionCheck']['ConditionExpression'], 'attribute_not_exists(pk)')

    def test_creation_and_step_use_owner_epoch_guard(self):
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='table'); store.client = Mock(); store.serializer = TypeSerializer(); store.auth_table_name = 'auth'
        pk = 'SCOPE#USER#owner#COMBI_MEAL#meal'
        store.create_meal(pk, [{'sk': 'META', 'version': 1}], 4)
        guard = store.client.transact_write_items.call_args.kwargs['TransactItems'][0]['ConditionCheck']
        self.assertEqual(guard['Key']['pk'], {'S': 'USER#owner'})
        self.assertEqual(guard['ExpressionAttributeValues'][':expected'], {'N': '4'})
        store.advance_meal(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'STEP#rice'}, {'sk': 'EVENT#1'}, 4)
        guard = store.client.transact_write_items.call_args.kwargs['TransactItems'][0]['ConditionCheck']
        self.assertIn('deletion_lock_request', guard['ConditionExpression'])
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
        store.create_meal('meal', [{'sk':'META','version':1}])
        store.append_meal_live('meal', 0, {'sk':'META','count':5}, {'sk':'CHUNK#000000'})
        with self.assertRaises(Conflict):
            store.attach_meal_source('meal', 1, {'sk':'META','version':2}, {'sk':'EVENT#2'}, [], None)
        self.assertIsNone(store.read('meal')[0].get('source'))
