import json
import unittest
from unittest.mock import Mock, patch

from foodtech_business_api import restatement_queue


class RestatementQueueTests(unittest.TestCase):
    def test_fifo_dedup_is_stable_and_scoped_to_job(self):
        sqs = Mock()
        with patch.object(restatement_queue, "_clients", return_value=(None, sqs)):
            restatement_queue.send("queue", "job-one", "same-id")
            restatement_queue.send("queue", "job-one", "same-id")
            restatement_queue.send("queue", "job-two", "same-id")
        first, second, other = [call.kwargs for call in sqs.send_message.call_args_list]
        self.assertEqual(first["MessageDeduplicationId"], second["MessageDeduplicationId"])
        self.assertNotEqual(first["MessageDeduplicationId"], other["MessageDeduplicationId"])
        self.assertEqual(json.loads(first["MessageBody"])["kind"], "CUSTOMER_RESTATEMENT")
        self.assertNotEqual(first["MessageGroupId"], other["MessageGroupId"])
