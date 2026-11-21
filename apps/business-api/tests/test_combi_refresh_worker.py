import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from foodtech_business_api.combi_refresh_worker import process_pending_marker, process_pending_page, RefreshNotAuthorized


class CombiRefreshWorkerTests(unittest.TestCase):
    def setUp(self):
        self.marker = {'pk': 'SCOPE#USER#customer-1#COMBI_MEAL#00000000-0000-4000-8000-000000000001',
                       'sk': 'MEAL_AGGREGATE#DIRTY'}
        self.auth = MagicMock()
        self.store = object()

    def test_valid_active_account_reads_consistently_and_passes_epoch(self):
        self.auth.get_item.return_value = {'Item': {'epoch': Decimal('3'), 'suspended': False}}
        with patch('foodtech_business_api.combi_refresh_worker.refresh_pending_for_subject',
                   new_callable=AsyncMock, return_value={'status': 'REFRESHED'}) as refresh:
            result = process_pending_marker(self.store, self.auth, self.marker)
        self.assertEqual(result['status'], 'REFRESHED')
        self.auth.get_item.assert_called_once_with(Key={'pk': 'USER#customer-1'}, ConsistentRead=True)
        refresh.assert_awaited_once_with(self.store, 'customer-1', 3, UUID('00000000-0000-4000-8000-000000000001'))

    def test_invalid_scope_never_reads_account(self):
        for marker in ({'pk': self.marker['pk'] + '#extra', 'sk': self.marker['sk']},
                       {'pk': self.marker['pk'], 'sk': 'META'},
                       {'pk': 'SCOPE#USER#other#COMBI_MEAL#not-a-uuid', 'sk': self.marker['sk']}):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                process_pending_marker(self.store, self.auth, marker)
        self.auth.get_item.assert_not_called()

    def test_missing_suspended_locked_or_invalid_epoch_never_refreshes(self):
        for principal in (None, {'suspended': True}, {'deletion_lock_request': 'lock'},
                          {'epoch': True}, {'epoch': Decimal('1.5')}, {'epoch': Decimal('NaN')},
                          {'epoch': -1}, {'epoch': 2**63}, {'epoch': '1'}):
            self.auth.get_item.return_value = {'Item': principal} if principal is not None else {}
            with self.subTest(principal=principal), patch(
                    'foodtech_business_api.combi_refresh_worker.refresh_pending_for_subject', new_callable=AsyncMock) as refresh:
                with self.assertRaises(RefreshNotAuthorized):
                    process_pending_marker(self.store, self.auth, self.marker)
                refresh.assert_not_awaited()

    def test_refresh_failure_is_propagated_for_retry(self):
        self.auth.get_item.return_value = {'Item': {}}
        with patch('foodtech_business_api.combi_refresh_worker.refresh_pending_for_subject',
                   new_callable=AsyncMock, side_effect=RuntimeError('retry')):
            with self.assertRaisesRegex(RuntimeError, 'retry'):
                process_pending_marker(self.store, self.auth, self.marker)


class CombiRefreshDiscoveryTests(unittest.TestCase):
    def test_page_processes_all_entries_and_reports_failure_without_skipping_cursor(self):
        store = MagicMock()
        rows = [{'pk': 'one', 'sk': 'MEAL_AGGREGATE#DIRTY'}, {'pk': 'two', 'sk': 'MEAL_AGGREGATE#DIRTY'}]
        cursor = {'pk': 'two', 'sk': 'MEAL_AGGREGATE#DIRTY', 'aggregate_refresh_shard': 'PENDING#03'}
        store.table.query.return_value = {'Items': rows, 'LastEvaluatedKey': cursor}
        with patch('foodtech_business_api.combi_refresh_worker.process_pending_marker',
                   side_effect=[RuntimeError('private data'), {'status': 'REFRESHED'}]) as process:
            result = process_pending_page(store, object(), 3, limit=2)
        self.assertEqual(result, {'processed': 1, 'failed': 1, 'next_cursor': cursor})
        self.assertEqual(process.call_count, 2)
        self.assertEqual(store.table.query.call_args.kwargs['IndexName'], 'MealAggregatePending')
        self.assertEqual(store.table.query.call_args.kwargs['Limit'], 2)

    def test_invalid_bounds_do_not_query(self):
        store = MagicMock()
        for shard, limit in [(-1, 1), (16, 1), (True, 1), (0, 0), (0, 101)]:
            with self.subTest(shard=shard, limit=limit), self.assertRaises(ValueError):
                process_pending_page(store, object(), shard, limit=limit)
        store.table.query.assert_not_called()
