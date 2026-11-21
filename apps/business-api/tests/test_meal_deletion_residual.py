import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.meal_deletion_residual import MealFinalResidualScan, MealResidualScan
from test_profile_deletion_inventory import Progress


class MealResidualTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.source = Mock(); self.source.name = 'cooking'; self.source.table_arn = 'arn:source'
        self.progress = Progress(); self.cipher = SimpleNamespace(source_table_arn='arn:source')
        self.scan = MealResidualScan(source_table=self.source, progress_store=self.progress, cipher=self.cipher)
        self.scope = {'subject': self.subject, 'request_id': self.request, 'limit': 20}

    def test_orphan_record_stops_and_rechecks_same_scan_position(self):
        row = {'pk': f'SCOPE#USER#{self.subject}#COMBI_MEAL#orphan', 'sk': 'EVENT#1'}
        self.source.scan.side_effect = [{'Items': [row], 'ScannedCount': 1}, {'Items': [], 'ScannedCount': 1}]
        self.assertEqual(self.scan.observe_page(**self.scope), {'scan_complete': False, 'residual_found': True})
        with self.assertRaises(ValueError):
            self.scan.repair_one(**self.scope)
        self.assertEqual(self.scan.observe_page(**self.scope), {'scan_complete': True, 'residual_found': False})
        self.assertNotIn('ExclusiveStartKey', self.source.scan.call_args.kwargs)

    def test_final_scan_detects_meta(self):
        scan = MealFinalResidualScan(source_table=self.source, progress_store=self.progress, cipher=self.cipher)
        self.source.scan.return_value = {'Items': [
            {'pk': f'SCOPE#USER#{self.subject}#COMBI_MEAL#late', 'sk': 'META'}], 'ScannedCount': 1}
        self.assertEqual(scan.observe_page(**self.scope), {'scan_complete': False, 'residual_found': True})

    def test_wrong_scope_or_invalid_key_is_not_treated_as_clean(self):
        for pk in (f'SCOPE#USER#{uuid4()}#COMBI_MEAL#meal',
                   f'SCOPE#USER#{self.subject}#COMBI_MEAL#bad#suffix'):
            self.source.scan.return_value = {'Items': [{'pk': pk, 'sk': 'EVENT#1'}], 'ScannedCount': 1}
            with self.assertRaises(ValueError):
                self.scan.observe_page(**self.scope)
        self.assertFalse(self.progress.items)


if __name__ == '__main__':
    unittest.main()
