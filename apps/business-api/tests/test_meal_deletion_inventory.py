import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.meal_deletion_inventory import MealDeletionInventory
from foodtech_business_api.meal_deletion_worker import MealDeletionWorker
from test_profile_deletion_inventory import Progress


class MealInventoryTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.source = Mock(); self.source.name = 'cooking'; self.source.table_arn = 'arn:source'
        self.progress = Progress(); self.cipher = SimpleNamespace(source_table_arn='arn:source')
        self.inventory = MealDeletionInventory(source_table=self.source, progress_store=self.progress, cipher=self.cipher)
        self.scope = {'subject': self.subject, 'request_id': self.request}
        self.row = {'pk': f'SCOPE#USER#{self.subject}#COMBI_MEAL#meal', 'sk': 'META', 'meal_batch_id': 'meal'}

    def test_all_pages_verified_before_yielding_targets(self):
        self.source.scan.side_effect = [
            {'Items': [], 'ScannedCount': 2, 'LastEvaluatedKey': {'pk': 'other', 'sk': 'key'}},
            {'Items': [self.row], 'ScannedCount': 1}]
        self.assertFalse(self.inventory.capture_page(**self.scope, limit=20)['complete'])
        self.assertEqual(self.inventory.capture_page(**self.scope, limit=20)['count'], 1)
        targets = list(self.inventory.targets(**self.scope))
        self.assertEqual(targets[0].meal_id, 'meal')
        self.assertNotIn('meal', repr(targets))
        del self.progress.items[(self.subject, self.request, 'MEAL_INVENTORY_PAGE_000000000002')]
        with self.assertRaises(ValueError):
            next(self.inventory.targets(**self.scope))

    def test_wrong_scope_and_malformed_meta_are_not_captured(self):
        for row in ({**self.row, 'pk': self.row['pk'] + '#extra'},
                    {**self.row, 'meal_batch_id': 'bad#id'},
                    {**self.row, 'pk': f'SCOPE#USER#{uuid4()}#COMBI_MEAL#meal'}):
            self.source.scan.return_value = {'Items': [row], 'ScannedCount': 1}
            with self.assertRaises(ValueError):
                self.inventory.capture_page(**self.scope, limit=20)
        self.assertFalse(self.progress.items)

    def test_captured_meal_is_cleaned_with_guarded_transaction(self):
        self.source.scan.return_value = {'Items': [self.row], 'ScannedCount': 1}
        self.source.query.return_value = {'Items': [
            {'pk': self.row['pk'], 'sk': 'EVENT#1'}, self.row]}
        self.progress.client = Mock()
        self.inventory.capture_page(**self.scope, limit=20)
        worker = MealDeletionWorker(inventory=self.inventory, source_table=self.source,
                                    progress_store=self.progress, cipher=self.cipher)
        self.assertEqual(worker.delete_one(**self.scope, index=0),
                         {'index': 0, 'state': 'IN_PROGRESS', 'removed': True})
        ops = self.progress.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[3]['Delete']['Key']['pk'], {'S': self.row['pk']})
        self.assertEqual(ops[3]['Delete']['Key']['sk'], {'S': 'EVENT#1'})


if __name__ == '__main__':
    unittest.main()
