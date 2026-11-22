import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.meal_aggregate_deletion_inventory import MealAggregateDeletionInventory
from foodtech_business_api.meal_aggregate_deletion_worker import MealAggregateDeletionWorker
from foodtech_business_api.meal_aggregate_group_deletion import clean_one_meal_aggregate_group
from foodtech_business_api.meal_aggregate_deletion_residual import MealAggregateResidualScan
from test_profile_deletion_inventory import Progress


class MealAggregateDeletionTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.source = Mock(); self.source.name = 'cooking'; self.source.table_arn = 'arn:source'
        self.progress = Progress(); self.progress.client = Mock()
        self.cipher = SimpleNamespace(source_table_arn='arn:source')
        self.inventory = MealAggregateDeletionInventory(source_table=self.source, progress_store=self.progress,
                                                        cipher=self.cipher)
        self.worker = MealAggregateDeletionWorker(inventory=self.inventory)
        self.scope = {'subject': self.subject, 'request_id': self.request}
        self.group, self.result = 'a' * 64, 'b' * 64
        self.row = {'pk': f'SCOPE#USER#{self.subject}#MEAL_AGGREGATE#{self.group}',
                    'sk': f'RESULT#{self.result}', 'group_id': self.group, 'result_id': self.result}

    def test_index_independent_capture_and_guarded_deletion(self):
        self.source.scan.return_value = {'Items': [self.row], 'ScannedCount': 1}
        self.assertTrue(self.inventory.capture_page(**self.scope, limit=20)['complete'])
        target = list(self.inventory.targets(**self.scope))[0]
        self.assertEqual((target.group_id, target.result_id), (self.group, self.result))
        self.assertNotIn(self.result, repr(target))
        self.assertEqual(self.worker.delete_one(**self.scope, index=0), {'index': 0, 'state': 'VERIFYING'})
        ops = self.progress.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[0]['ConditionCheck']['ExpressionAttributeValues'][':request'], {'S': self.request})
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        self.assertEqual(ops[2]['Delete']['Key']['pk'], {'S': self.row['pk']})
        self.assertEqual(ops[2]['Delete']['Key']['sk'], {'S': self.row['sk']})

    def test_bad_scope_or_missing_page_blocks_cleanup(self):
        self.source.scan.return_value = {'Items': [{**self.row, 'pk': self.row['pk'] + 'x'}], 'ScannedCount': 1}
        with self.assertRaises(ValueError):
            self.inventory.capture_page(**self.scope, limit=20)
        self.source.scan.return_value = {'Items': [self.row], 'ScannedCount': 1}
        self.inventory.capture_page(**self.scope, limit=20)
        del self.progress.items[(self.subject, self.request, 'MEAL_AGGREGATE_PAGE_000000000001')]
        with self.assertRaises(ValueError):
            self.worker.delete_one(**self.scope, index=0)
        self.progress.client.transact_write_items.assert_not_called()

    def test_changed_lock_is_not_accepted(self):
        self.source.scan.return_value = {'Items': [self.row], 'ScannedCount': 1}
        self.inventory.capture_page(**self.scope, limit=20)
        self.progress.client.transact_write_items.side_effect = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):
            self.worker.delete_one(**self.scope, index=0)

    def test_batch_checks_complete_inventory_and_can_retry_after_partial_failure(self):
        other = {**self.row, 'sk': 'RESULT#' + 'c' * 64, 'result_id': 'c' * 64}
        self.source.scan.return_value = {'Items': [self.row, other], 'ScannedCount': 2}
        self.inventory.capture_page(**self.scope, limit=20)
        self.progress.client.transact_write_items.side_effect = [None, RuntimeError('temporary failure')]
        with self.assertRaises(RuntimeError):
            self.worker.delete_batch(**self.scope, start=0, count=2)
        self.assertEqual(self.progress.client.transact_write_items.call_count, 2)
        self.progress.client.transact_write_items.reset_mock(side_effect=True)
        self.assertEqual(self.worker.delete_batch(**self.scope, start=0, count=2),
                         {'count': 2, 'state': 'VERIFYING'})
        self.assertEqual(self.progress.client.transact_write_items.call_count, 2)
        self.progress.client.transact_write_items.reset_mock()
        with self.assertRaises(ValueError):
            self.worker.delete_batch(**self.scope, start=0, count=3)
        self.progress.client.transact_write_items.assert_not_called()

    def test_batch_stops_when_deletion_request_changes_midway(self):
        rows = [{**self.row, 'sk': 'RESULT#' + f'{index:064x}', 'result_id': f'{index:064x}'}
                for index in (1, 2, 3)]
        self.source.scan.return_value = {'Items': rows, 'ScannedCount': 3}
        self.inventory.capture_page(**self.scope, limit=20)
        conflict = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'None'}, {'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        self.progress.client.transact_write_items.side_effect = [None, conflict]
        with self.assertRaises(ProfileDeletionConflict):
            self.worker.delete_batch(**self.scope, start=0, count=3)
        self.assertEqual(self.progress.client.transact_write_items.call_count, 2)
        for call in self.progress.client.transact_write_items.call_args_list:
            checks = call.kwargs['TransactItems']
            self.assertEqual(checks[0]['ConditionCheck']['ExpressionAttributeValues'][':request'], {'S': self.request})
            self.assertEqual(checks[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})

    def test_group_index_remains_until_result_partition_is_empty(self):
        index = {'pk': f'SCOPE#USER#{self.subject}#MEAL_AGGREGATE_GROUPS',
                 'sk': f'GROUP#{self.group}', 'group_id': self.group}
        args = {**self.scope, 'source_table': self.source, 'progress_store': self.progress, 'cipher': self.cipher}
        self.source.query.side_effect = [{'Items': [index]}, {'Items': [self.row]}]
        with self.assertRaises(ValueError):
            clean_one_meal_aggregate_group(**args)
        self.progress.client.transact_write_items.assert_not_called()
        self.source.query.side_effect = [{'Items': [index]}, {'Items': []}]
        self.assertEqual(clean_one_meal_aggregate_group(**args), {'state': 'IN_PROGRESS', 'removed': True})
        ops = self.progress.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[2]['Delete']['Key']['pk'], {'S': index['pk']})
        self.assertEqual(ops[2]['Delete']['ExpressionAttributeValues'][':group'], {'S': self.group})
        self.source.query.side_effect = [{'Items': []}]
        self.assertEqual(clean_one_meal_aggregate_group(**args), {'state': 'VERIFYING', 'removed': False})

    def test_late_or_unexpected_result_row_blocks_residual_scan(self):
        scan = MealAggregateResidualScan(source_table=self.source, progress_store=self.progress, cipher=self.cipher)
        self.source.scan.side_effect = [{'Items': [{**self.row, 'sk': 'UNEXPECTED'}], 'ScannedCount': 1},
                                        {'Items': [], 'ScannedCount': 1}]
        self.assertEqual(scan.observe_page(**self.scope, limit=20), {'scan_complete': False, 'residual_found': True})
        with self.assertRaises(ValueError):
            scan.repair_one(**self.scope)
        self.assertEqual(scan.observe_page(**self.scope, limit=20), {'scan_complete': True, 'residual_found': False})
        self.assertNotIn('ExclusiveStartKey', self.source.scan.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
