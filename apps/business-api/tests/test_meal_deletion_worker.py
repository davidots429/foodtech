import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_business_api.meal_deletion_worker import MealDeletionWorker


class MealWorkerTests(unittest.TestCase):
    def setUp(self):
        self.source, self.progress, self.cipher = object(), object(), object()
        self.inventory = SimpleNamespace(source_table=self.source, progress_store=self.progress, cipher=self.cipher,
            targets=Mock(return_value=iter([SimpleNamespace(meal_id='captured')])))
        self.worker = MealDeletionWorker(inventory=self.inventory, source_table=self.source,
                                         progress_store=self.progress, cipher=self.cipher)
        self.scope = {'subject': str(uuid4()), 'request_id': str(uuid4())}

    def test_only_verified_target_is_passed_to_cleanup(self):
        with patch('foodtech_business_api.meal_deletion_worker.delete_one_meal_record',
                   return_value={'state': 'META_ONLY', 'removed': False}) as delete:
            self.assertEqual(self.worker.delete_one(**self.scope, index=0),
                             {'index': 0, 'state': 'META_ONLY', 'removed': False})
        self.assertEqual(delete.call_args.kwargs['meal_id'], 'captured')

    def test_missing_target_or_wrong_dependencies_fail(self):
        self.inventory.targets = Mock(return_value=iter([]))
        with patch('foodtech_business_api.meal_deletion_worker.delete_one_meal_record') as delete:
            with self.assertRaises(ValueError):
                self.worker.delete_one(**self.scope, index=0)
            delete.assert_not_called()
        with self.assertRaises(ValueError):
            MealDeletionWorker(inventory=self.inventory, source_table=object(),
                               progress_store=self.progress, cipher=self.cipher)

    def test_bounded_records_reuse_verified_target_and_stop_at_limit(self):
        with patch('foodtech_business_api.meal_deletion_worker.delete_one_meal_record',
                   return_value={'state': 'IN_PROGRESS', 'removed': True}) as delete:
            result = self.worker.delete_records(**self.scope, index=0, limit=2)
        self.assertEqual(result, {'index': 0, 'state': 'IN_PROGRESS', 'removed': True})
        self.assertEqual(delete.call_count, 2)
        self.assertEqual(self.inventory.targets.call_count, 1)
        with self.assertRaises(ValueError): self.worker.delete_records(**self.scope, index=0, limit=0)


if __name__ == '__main__':
    unittest.main()
