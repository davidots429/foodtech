import unittest
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.tier_deletion_worker import TierDeletionWorker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tier = Mock()
        self.progress = Mock()
        self.cipher = object()
        self.worker = TierDeletionWorker(tier_store=self.tier, progress_store=self.progress, cipher=self.cipher)
        self.scope = {'subject': str(uuid4()), 'request_id': str(uuid4()), 'limit': 2}

    def test_restartable_pages_end_in_verification_not_completion(self):
        self.progress.load_checkpoint.side_effect = [None, {'version': 1, 'state': 'IN_PROGRESS',
                                                            'checkpoint': b'tier-first-page-v1'}]
        self.progress.save_checkpoint.side_effect = [1, 2]
        self.tier.delete_inventory_page.side_effect = [2, 0]
        self.assertEqual(self.worker.run_page(**self.scope), {'deleted_count': 2, 'version': 1, 'state': 'IN_PROGRESS'})
        self.assertEqual(self.worker.run_page(**self.scope), {'deleted_count': 0, 'version': 2, 'state': 'VERIFYING'})
        self.assertEqual(self.tier.delete_inventory_page.call_count, 2)
        self.assertEqual(self.progress.save_checkpoint.call_args.kwargs['expected_version'], 1)
        self.assertEqual(self.progress.save_checkpoint.call_args.kwargs['domain'], 'TIER')

    def test_partial_failure_never_advances_and_bad_checkpoint_never_deletes(self):
        self.progress.load_checkpoint.return_value = None
        self.tier.delete_inventory_page.side_effect = RuntimeError('conflict')
        with self.assertRaises(RuntimeError):
            self.worker.run_page(**self.scope)
        self.progress.save_checkpoint.assert_not_called()
        self.tier.delete_inventory_page.reset_mock(side_effect=True)
        self.progress.load_checkpoint.return_value = {'version': 1, 'checkpoint': b'other'}
        with self.assertRaises(ValueError):
            self.worker.run_page(**self.scope)
        self.tier.delete_inventory_page.assert_not_called()

    def test_progress_write_conflict_does_not_report_success(self):
        self.progress.load_checkpoint.return_value = None
        self.tier.delete_inventory_page.return_value = 2
        self.progress.save_checkpoint.side_effect = RuntimeError('conflict')
        with self.assertRaises(RuntimeError):
            self.worker.run_page(**self.scope)


if __name__ == '__main__':
    unittest.main()
