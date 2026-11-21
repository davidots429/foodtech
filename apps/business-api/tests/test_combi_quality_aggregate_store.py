import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.cooking_store import DynamoCookingStore


class MealAggregateStoreTests(unittest.TestCase):
    def test_transaction_checks_all_selected_meal_versions(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='cooking')
        store.client = MagicMock()
        store.serializer = TypeSerializer()
        record = {'result_id': 'a' * 64, 'selection': {}, 'references': []}
        store.save_meal_aggregate('aggregate', [('meal-a', 3), ('meal-b', 7)], record)
        ops = store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(ops), 3)
        self.assertEqual(ops[0]['ConditionCheck']['Key']['pk'], {'S': 'meal-a'})
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':version'], {'N': '7'})
        self.assertEqual(ops[2]['Put']['ConditionExpression'], 'attribute_not_exists(pk)')


if __name__ == '__main__':
    unittest.main()
