import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_ai_gateway.tenant_partition_worker import TenantPartitionWorker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.table, self.progress, self.cipher = Mock(), Mock(), object()
        self.progress.auth_table, self.progress.client = 'auth', object()
        self.progress.load_checkpoint.return_value = None
        self.progress.save_checkpoint.return_value = 1
        self.capture = SimpleNamespace(source_table=self.table, progress_store=self.progress, cipher=self.cipher,
                                       partitions=lambda **kw: iter([SimpleNamespace(partition='EXEC#run')]))
        self.worker = TenantPartitionWorker(capture=self.capture, progress_store=self.progress,
                                            cipher=self.cipher, source_table=self.table)
        self.scope = {'subject': str(uuid4()), 'request_id': str(uuid4()), 'index': 0, 'limit': 2}
        self.table.get_item.return_value = {'Item': {'pk': 'EXEC#run', 'sk': 'META'}}

    def test_page_progress_records_cursor_without_exposing_partition(self):
        result = {'deleted_children': 2, 'next_cursor': 'CALL#2', 'partition_removed': False}
        with patch('foodtech_ai_gateway.tenant_partition_worker.delete_registry_partition_page', return_value=result):
            output = self.worker.run_page(**self.scope)
        self.assertEqual(output, {'index': 0, 'version': 1, 'state': 'IN_PROGRESS', 'deleted_children': 2})
        self.assertNotIn('EXEC#run', repr(output))
        self.assertEqual(json.loads(self.progress.save_checkpoint.call_args.kwargs['checkpoint'])['cursor'], 'CALL#2')

    def test_lost_final_response_checks_absence_before_progress(self):
        self.table.get_item.return_value = {}
        with patch('foodtech_ai_gateway.tenant_partition_worker.delete_registry_key') as delete:
            output = self.worker.run_page(**self.scope)
        self.assertEqual(output['state'], 'VERIFYING')
        self.assertEqual(delete.call_args.kwargs['sk'], 'META')

    def test_credentials_and_failed_deletion_cannot_advance(self):
        self.capture.partitions = lambda **kw: iter([SimpleNamespace(partition='CREDENTIAL#secret')])
        with self.assertRaises(ValueError):
            self.worker.run_page(**self.scope)
        self.progress.save_checkpoint.assert_not_called()
        self.capture.partitions = lambda **kw: iter([SimpleNamespace(partition='EXEC#run')])
        with patch('foodtech_ai_gateway.tenant_partition_worker.delete_registry_partition_page', side_effect=RuntimeError('locked')):
            with self.assertRaises(RuntimeError):
                self.worker.run_page(**self.scope)
        self.progress.save_checkpoint.assert_not_called()


if __name__ == '__main__':
    unittest.main()
