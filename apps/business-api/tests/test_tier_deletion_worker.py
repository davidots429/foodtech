import unittest
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.tier_deletion_worker import TierDeletionWorker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tier = Mock()
        self.progress = Mock()
        self.cipher = object()
        self.index_capture = Mock()
        self.worker = TierDeletionWorker(tier_store=self.tier, progress_store=self.progress, cipher=self.cipher,
                                         index_capture=self.index_capture)
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
        self.progress.load_checkpoint.return_value = {'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': b'other'}
        with self.assertRaises(ValueError):
            self.worker.run_page(**self.scope)
        self.tier.delete_inventory_page.assert_not_called()

    def test_invalid_page_or_progress_never_calls_delete(self):
        for limit in (0, 101, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.worker.run_page(**{**self.scope, 'limit': limit})
        self.progress.load_checkpoint.assert_not_called()
        for previous in ({'version': True, 'state': 'IN_PROGRESS', 'checkpoint': b'tier-first-page-v1'},
                         {'version': 1, 'state': 'COMPLETED', 'checkpoint': b'tier-first-page-v1'}):
            self.progress.load_checkpoint.return_value = previous
            with self.subTest(previous=previous), self.assertRaises(ValueError):
                self.worker.run_page(**self.scope)
        self.tier.delete_inventory_page.assert_not_called()

    def test_retry_after_progress_save_failure_starts_from_remaining_keys(self):
        remaining = ['a', 'b', 'c']
        self.progress.load_checkpoint.return_value = None
        self.progress.save_checkpoint.side_effect = [RuntimeError('write failed'), 1, 2]
        def delete_page(subject, request_id, limit):
            removed = min(len(remaining), limit)
            del remaining[:removed]
            return removed
        self.tier.delete_inventory_page.side_effect = delete_page
        with self.assertRaises(RuntimeError):
            self.worker.run_page(**self.scope)
        self.assertEqual(remaining, ['c'])
        self.assertEqual(self.worker.run_page(**self.scope)['deleted_count'], 1)
        self.progress.load_checkpoint.return_value = {'version': 1, 'state': 'IN_PROGRESS',
                                                       'checkpoint': b'tier-first-page-v1'}
        self.assertEqual(self.worker.run_page(**self.scope)['state'], 'VERIFYING')
        self.assertEqual(remaining, [])

    def test_progress_write_conflict_does_not_report_success(self):
        self.progress.load_checkpoint.return_value = None
        self.tier.delete_inventory_page.return_value = 2
        self.progress.save_checkpoint.side_effect = RuntimeError('conflict')
        with self.assertRaises(RuntimeError):
            self.worker.run_page(**self.scope)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        from foodtech_business_api.tier_deletion_worker import TierIndexCapture
        self.tier, self.progress, self.cipher = Mock(), Mock(), object()
        self.capture = TierIndexCapture(tier_store=self.tier, progress_store=self.progress, cipher=self.cipher)
        self.scope = {'subject': str(uuid4()), 'request_id': str(uuid4())}

    def test_capture_pages_are_saved_before_ready(self):
        self.progress.load_checkpoint.return_value = None
        self.progress.save_checkpoint.return_value = 1
        self.tier.deletion_index_candidates.return_value = {'queue_sorts': ['date#a'], 'next_cursor': 'APPLICATION#a'}
        first = self.capture.capture_page(**self.scope, limit=1)
        self.assertFalse(first['complete'])
        payload = self.progress.save_checkpoint.call_args.kwargs['checkpoint']
        self.progress.load_checkpoint.return_value = {'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': payload}
        with self.assertRaises(ValueError):
            self.capture.ensure_ready(**self.scope)
        self.tier.deletion_index_candidates.return_value = {'queue_sorts': ['date#b'], 'next_cursor': None}
        self.progress.save_checkpoint.return_value = 2
        final = self.capture.capture_page(**self.scope, limit=1)
        self.assertTrue(final['complete'])
        self.assertEqual(final['captured_count'], 2)
        self.assertNotIn('date#a', repr(final))
        payload = self.progress.save_checkpoint.call_args.kwargs['checkpoint']
        self.progress.load_checkpoint.return_value = {'version': 2, 'state': 'VERIFYING', 'checkpoint': payload}
        self.capture.ensure_ready(**self.scope)

    def test_failed_save_and_oversized_page_do_not_become_ready(self):
        self.progress.load_checkpoint.return_value = None
        self.tier.deletion_index_candidates.return_value = {'queue_sorts': ['x' * 2048] * 10, 'next_cursor': None}
        with self.assertRaises(ValueError):
            self.capture.capture_page(**self.scope, limit=10)
        self.progress.save_checkpoint.assert_not_called()
        self.tier.deletion_index_candidates.return_value = {'queue_sorts': ['date#a'], 'next_cursor': None}
        self.progress.save_checkpoint.side_effect = RuntimeError('conflict')
        with self.assertRaises(RuntimeError):
            self.capture.capture_page(**self.scope, limit=1)
        with self.assertRaises(ValueError):
            self.capture.ensure_ready(**self.scope)

    def test_deletion_refuses_incomplete_capture(self):
        self.progress.load_checkpoint.return_value = None
        worker = TierDeletionWorker(tier_store=self.tier, progress_store=self.progress,
                                    cipher=self.cipher, index_capture=self.capture)
        with self.assertRaises(ValueError):
            worker.run_page(**self.scope, limit=1)
        self.tier.delete_inventory_page.assert_not_called()


if __name__ == '__main__':
    unittest.main()
