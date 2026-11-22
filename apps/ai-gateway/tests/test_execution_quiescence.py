import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_ai_gateway.execution_quiescence import ExecutionQuiescence


class QuiescenceTests(unittest.TestCase):
    def setUp(self):
        self.table, self.progress, self.cipher = Mock(), Mock(auth_table='auth', client=object()), object()
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.capture = SimpleNamespace(source_table=self.table, progress_store=self.progress, cipher=self.cipher,
            partitions=lambda **kw: iter([SimpleNamespace(partition='EXEC#one'), SimpleNamespace(partition='CONSENT#x')]))
        self.worker = ExecutionQuiescence(capture=self.capture, progress_store=self.progress,
                                          cipher=self.cipher, source_table=self.table)
        self.table.get_item.return_value = {'Item': {'pk': 'EXEC#one', 'sk': 'META',
                                                    'tenant_id': self.subject, 'state': 'SUCCEEDED'}}

    @patch('foodtech_ai_gateway.execution_quiescence.observe_active_markers')
    def test_marker_and_reservation_block_and_clear_is_observation_only(self, markers):
        markers.return_value = 'PRESENT'
        self.assertEqual(self.worker.observe(subject=self.subject, request_id=self.request)['reason'], 'ACTIVE_MARKER')
        markers.return_value = 'CLEAR'
        self.table.get_item.return_value['Item']['state'] = 'RESERVED'
        self.assertEqual(self.worker.observe(subject=self.subject, request_id=self.request)['reason'], 'RESERVED_EXECUTION')
        self.table.get_item.return_value['Item']['state'] = 'SUCCEEDED'
        self.assertEqual(self.worker.observe(subject=self.subject, request_id=self.request),
                         {'observation': 'CLEAR_OBSERVED', 'checked_executions': 1})
        markers.side_effect = ['CLEAR', 'PRESENT']
        self.assertEqual(self.worker.observe(subject=self.subject, request_id=self.request)['reason'], 'ACTIVE_MARKER')

    @patch('foodtech_ai_gateway.execution_quiescence.observe_active_markers', return_value='CLEAR')
    def test_missing_parent_requires_empty_partition_and_owner_must_match(self, _):
        self.table.get_item.return_value = {}
        self.table.query.return_value = {'Items': [{'pk': 'EXEC#one', 'sk': 'RESULT'}]}
        with self.assertRaises(ValueError):
            self.worker.observe(subject=self.subject, request_id=self.request)
        self.table.query.return_value = {'Items': []}
        self.assertEqual(self.worker.observe(subject=self.subject, request_id=self.request)['observation'], 'CLEAR_OBSERVED')
        self.table.get_item.return_value = {'Item': {'pk':'EXEC#one','sk':'META','tenant_id':'other','state':'SUCCEEDED'}}
        with self.assertRaises(ValueError):
            self.worker.observe(subject=self.subject, request_id=self.request)


if __name__ == '__main__':
    unittest.main()
