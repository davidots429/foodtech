import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker


class CustomerRestatementDispatchTests(unittest.TestCase):
    def setUp(self):
        self.worker = DynamoStandardizationWorker.__new__(DynamoStandardizationWorker)
        self.request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.job_pk = "SCOPE#USER#synthetic#JOB#test"
        self.table = Mock()
        self.table.get_item.return_value = {"Item": {"reason": "CUSTOMER_REFRESH", "request_id": self.request_id}}
        self.restatement = Mock()
        self.restatement.process.return_value = "DERIVED"
        self.worker.process_bridge = SimpleNamespace(table=self.table, restatement=self.restatement)
        self.reference = {"kind": "CUSTOMER_RESTATEMENT", "job_pk": self.job_pk, "request_id": self.request_id}

    def test_trusted_request_is_processed_and_replay_is_idempotent(self):
        self.assertEqual(self.worker.process(self.reference)["result"], "DERIVED")
        self.restatement.process.assert_called_once_with(self.job_pk, self.request_id)
        self.restatement.process.return_value = "ALREADY_COMPLETED"
        self.assertEqual(self.worker.process(self.reference)["result"], "ALREADY_COMPLETED")

    def test_unknown_request_is_rejected_and_transient_result_retried(self):
        self.table.get_item.return_value = {}
        with self.assertRaises(ValueError): self.worker.process(self.reference)
        self.restatement.process.assert_not_called()
        self.table.get_item.return_value = {"Item": {"reason": "CUSTOMER_REFRESH", "request_id": self.request_id}}
        self.restatement.process.return_value = "STALE_RETRY"
        with self.assertRaises(RuntimeError): self.worker.process(self.reference)
