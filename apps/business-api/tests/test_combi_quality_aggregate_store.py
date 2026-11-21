import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.cooking_store import DynamoCookingStore


class MealAggregateStoreTests(unittest.TestCase):
    def test_transaction_checks_all_selected_meal_versions(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='cooking')
        store.client = MagicMock()
        store.serializer = TypeSerializer()
        store.auth_table_name = 'auth'
        record = {'group_id': 'b' * 64, 'result_id': 'a' * 64, 'selection': {}, 'references': []}
        subject = 'customer'; group_pk = f"SCOPE#USER#{subject}#MEAL_AGGREGATE#{record['group_id']}"
        meal_a, meal_b = (f'SCOPE#USER#{subject}#COMBI_MEAL#{uuid4()}' for _ in range(2))
        store.save_meal_aggregate(group_pk, [(meal_a, 3), (meal_b, 7)], record, subject, 4)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(ops), 7)
        self.assertIn('attribute_not_exists(deletion_lock_request)', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[0]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N': '4'})
        self.assertEqual(ops[1]['ConditionCheck']['Key']['pk'], {'S': meal_a})
        self.assertEqual(ops[2]['ConditionCheck']['ExpressionAttributeValues'][':version'], {'N': '7'})
        self.assertEqual(ops[3]['Put']['Item']['sk'], {'S': f"MEAL_AGGREGATE#DEP#{record['group_id']}"})
        self.assertEqual(ops[4]['Put']['Item']['pk'], {'S': meal_b})
        self.assertEqual(ops[5]['Put']['Item']['pk'], {'S': f'SCOPE#USER#{subject}#MEAL_AGGREGATE_GROUPS'})
        self.assertEqual(ops[6]['Put']['ConditionExpression'], 'attribute_not_exists(pk)')

    def test_other_customer_and_missing_auth_table_are_rejected(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='cooking'); store.client = MagicMock(); store.serializer = TypeSerializer()
        store.auth_table_name = ''
        record = {'group_id': 'b' * 64, 'result_id': 'a' * 64, 'selection': {}, 'references': []}
        subject = 'customer'; pk = f"SCOPE#USER#{subject}#MEAL_AGGREGATE#{record['group_id']}"
        meal = f'SCOPE#USER#{subject}#COMBI_MEAL#{uuid4()}'
        with self.assertRaises(ValueError):
            store.save_meal_aggregate(pk, [(meal, 1)], record, subject, 0)
        store.auth_table_name = 'auth'
        with self.assertRaises(ValueError):
            store.save_meal_aggregate(pk, [(f'SCOPE#USER#other#COMBI_MEAL#{uuid4()}', 1)], record, subject, 0)
        store.client.transact_write_items.assert_not_called()

    def test_acknowledge_checks_account_meta_and_exact_pending_version(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='cooking'); store.client = MagicMock(); store.serializer = TypeSerializer()
        store.auth_table_name = 'auth'
        subject = 'customer'; pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{uuid4()}'
        store.acknowledge_meal_aggregate_dirty(pk, 3, subject, 7)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertIn('deletion_lock_request', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':version'], {'N': '3'})
        self.assertEqual(ops[2]['Delete']['ExpressionAttributeValues'][':version'], {'N': '3'})
        self.assertEqual(ops[2]['Delete']['ExpressionAttributeValues'][':pending'], {'S': 'PENDING'})


    def test_checkpoint_requires_account_version_and_compare_and_set_cursor(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='cooking'); store.client = MagicMock(); store.serializer = TypeSerializer()
        store.auth_table_name = 'auth'
        subject = 'customer'; pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{uuid4()}'
        store.checkpoint_meal_aggregate_dirty(pk, 3, subject, 7, None, 'a' * 64)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertIn('deletion_lock_request', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':version'], {'N': '3'})
        self.assertIn('attribute_not_exists(#cursor)', ops[2]['Update']['ConditionExpression'])
        self.assertEqual(ops[2]['Update']['ExpressionAttributeValues'][':next'], {'S': 'a' * 64})
        store.acknowledge_meal_aggregate_dirty(pk, 3, subject, 7, 'a' * 64)
        operation = store.client.transact_write_items.call_args.kwargs['TransactItems'][2]['Delete']
        self.assertIn('#cursor = :cursor', operation['ConditionExpression'])
        with self.assertRaises(ValueError):
            store.checkpoint_meal_aggregate_dirty(pk, 3, subject, 7, 'b' * 64, 'a' * 64)
        with self.assertRaises(ValueError):
            store.checkpoint_meal_aggregate_dirty(pk, 3, subject, 7, None, None)


if __name__ == '__main__':
    unittest.main()
