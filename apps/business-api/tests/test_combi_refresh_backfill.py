import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from foodtech_business_api.combi_refresh_backfill import backfill_pending_page
from foodtech_business_api.cooking_store import meal_aggregate_shard


class ConditionalFailure(Exception):
    pass


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.table = MagicMock()
        self.table.meta.client.exceptions.ConditionalCheckFailedException = ConditionalFailure
        self.pk = 'SCOPE#USER#customer-1#COMBI_MEAL#00000000-0000-4000-8000-000000000001'
        self.row = {'pk': self.pk, 'sk': 'MEAL_AGGREGATE#DIRTY', 'version': Decimal('2'), 'state': 'PENDING'}

    def test_valid_marker_updates_with_version_state_and_absent_shard_guards(self):
        self.table.scan.return_value = {'Items': [self.row], 'LastEvaluatedKey': {'pk': 'next'}}
        self.assertEqual(backfill_pending_page(self.table, limit=5),
                         {'updated': 1, 'changed': 0, 'next_cursor': {'pk': 'next'}})
        call = self.table.update_item.call_args.kwargs
        self.assertEqual(call['Key'], {'pk': self.pk, 'sk': self.row['sk']})
        self.assertEqual(call['ExpressionAttributeValues'][':shard'], meal_aggregate_shard(self.pk))
        condition = call['ConditionExpression'].get_expression()
        self.assertEqual(condition['operator'], 'AND')
        self.assertTrue(self.table.scan.call_args.kwargs['ConsistentRead'])

    def test_concurrent_change_is_counted_without_retrying_stale_version(self):
        self.table.scan.return_value = {'Items': [self.row]}
        self.table.update_item.side_effect = ConditionalFailure()
        self.assertEqual(backfill_pending_page(self.table), {'updated': 0, 'changed': 1, 'next_cursor': None})
        self.table.update_item.assert_called_once()

    def test_invalid_item_and_cursor_fail_before_update(self):
        self.table.scan.return_value = {'Items': [{**self.row, 'version': Decimal('NaN')}]}
        with self.assertRaises(ValueError):
            backfill_pending_page(self.table)
        self.table.update_item.assert_not_called()
        with self.assertRaises(ValueError):
            backfill_pending_page(self.table, cursor='invalid')
