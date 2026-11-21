"""Regression for bounded, fully verified synthetic aggregate inventories."""
import unittest
from unittest.mock import Mock

from foodtech_business_api.meal_aggregate_deletion_inventory import MealAggregateDeletionInventory
from foodtech_business_api.meal_aggregate_deletion_worker import MealAggregateDeletionWorker


class AggregateDeletionScaleTests(unittest.TestCase):
    def test_thousand_targets_verify_pages_once_per_bounded_batch(self):
        inventory = MealAggregateDeletionInventory(source_table=None, progress_store=None, cipher=None)
        pages = {}
        for number in range(1, 52):
            start = None if number == 1 else {'pk': 'scan', 'sk': str(number - 1)}
            end = None if number == 51 else {'pk': 'scan', 'sk': str(number)}
            references = [[f'{i:064x}', f'{i + 1001:064x}']
                          for i in range((number - 1) * 20, min(number * 20, 1001))]
            pages[inventory._domain(number)] = {'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': inventory._pack(
                {'schema': 1, 'start': start, 'next': end, 'references': references})}
        pages[inventory.HEAD_DOMAIN] = {'version': 51, 'state': 'VERIFYING', 'checkpoint': inventory._pack(
            {'schema': 1, 'pages': 51, 'count': 1001, 'cursor': None, 'complete': True})}
        loads = []
        def load(subject, request_id, domain):
            loads.append(domain)
            return pages[domain]
        inventory._load = load
        worker = MealAggregateDeletionWorker(inventory=inventory)
        worker._delete_target = Mock()
        for start in range(0, 1001, 20):
            count = min(20, 1001 - start)
            before = len(loads)
            self.assertEqual(worker.delete_batch(subject='synthetic', request_id='synthetic', start=start, count=count),
                             {'count': count, 'state': 'VERIFYING'})
            self.assertTrue({inventory._domain(number) for number in range(1, 52)}.issubset(loads[before:]))
        self.assertEqual(worker._delete_target.call_count, 1001)
        self.assertLessEqual(len(loads), 4000)


if __name__ == '__main__':
    unittest.main()
