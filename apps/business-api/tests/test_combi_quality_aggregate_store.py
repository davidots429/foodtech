import unittest

import boto3
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore


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

    def test_strict_owner_fence_is_shared_by_result_checkpoint_and_acknowledgement(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='cooking')
        store.client = MagicMock()
        store.serializer = TypeSerializer()
        store.auth_table_name = 'auth'
        subject = 'customer'
        meal = f'SCOPE#USER#{subject}#COMBI_MEAL#{uuid4()}'
        record = {'group_id': 'b' * 64, 'result_id': 'a' * 64, 'selection': {}, 'references': []}
        group = f"SCOPE#USER#{subject}#MEAL_AGGREGATE#{record['group_id']}"
        actions = [lambda: store.save_meal_aggregate(group, [(meal, 3)], record, subject, 0),
                   lambda: store.checkpoint_meal_aggregate_dirty(meal, 3, subject, 0, None, 'a' * 64),
                   lambda: store.acknowledge_meal_aggregate_dirty(meal, 3, subject, 0)]
        # Check both modes: legacy omission remains compatible only when opted out.
        for strict in (False, True):
            store.require_principal = strict
            for action in actions:
                with self.subTest(strict=strict, action=action):
                    action()
                    guard = store.client.transact_write_items.call_args.kwargs['TransactItems'][0]['ConditionCheck']
                    self.assertEqual(guard['Key'], {'pk': {'S': 'USER#customer'}})
                    self.assertIn('attribute_exists(pk)', guard['ConditionExpression'])
                    self.assertIn('deletion_lock_request', guard['ConditionExpression'])
                    self.assertIn('suspended', guard['ConditionExpression'])
                    self.assertEqual('attribute_not_exists(#epoch)' in guard['ConditionExpression'], not strict)
                    self.assertEqual(':zero' in guard['ExpressionAttributeValues'], not strict)
        # An account changed after the worker read must cancel the whole write.
        client = boto3.client('dynamodb', region_name='ap-southeast-2',
                              aws_access_key_id='testing', aws_secret_access_key='testing')
        canceled = client.exceptions.TransactionCanceledException
        store.client.exceptions.TransactionCanceledException = canceled
        for action, count in zip(actions, (5, 3, 3)):
            store.client.transact_write_items.reset_mock()
            store.client.transact_write_items.side_effect = canceled(
                {'Error': {'Code': 'TransactionCanceledException', 'Message': 'owner changed'},
                 'CancellationReasons': [{'Code': 'ConditionalCheckFailed'},
                                         *[{'Code': 'None'} for _ in range(count - 1)]]},
                'TransactWriteItems')
            with self.assertRaises(Conflict):
                action()
            store.client.transact_write_items.assert_called_once()


if __name__ == '__main__':
    unittest.main()
