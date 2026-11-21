import json
import unittest
from unittest.mock import Mock

from foodtech_collection_adapter.jeyuk_aws import DynamoJeyukIngestor


def sample(second, temperature=190):
    return json.dumps({"fixtureId": "revision-test", "provenance": "SYNTHETIC", "profileId": "mockprofile-110",
                       "instanceId": "mockprofile-110-a-001", "profile": "SMALL", "actualLoadKg": 1.8,
                       "second": second, "sampleIntervalSeconds": 1, "surfaceCelsius": temperature,
                       "actualRpm": 30, "targetRpm": 30, "motorLoadPercent": 40})


class Cancelled(Exception):
    def __init__(self, reason=None):
        self.response = {"CancellationReasons": [{"Code": reason}]} if reason else {}


class Table:
    name = "test"
    def __init__(self):
        self.meta = self; self.client = self; self.exceptions = self
        self.TransactionCanceledException = Cancelled
        self.rows = {}; self.revision = 0; self.fail = False; self.conflicts = 0
    def transact_write_items(self, *, TransactItems):
        item = TransactItems[0]["Put"]["Item"]
        assert TransactItems[1]["Update"]["Key"] == {"pk": item["pk"], "sk": "SOURCE#REVISION"}
        if self.conflicts:
            self.conflicts -= 1
            raise Cancelled("TransactionConflict")
        if self.fail or item["sk"] in self.rows:
            raise Cancelled()
        self.rows[item["sk"]] = item
        self.revision += 1
    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead
        return {"Item": self.rows[Key["sk"]]} if Key["sk"] in self.rows else {}


class SourceRevisionTests(unittest.TestCase):
    def test_duplicate_does_not_increment_and_conflict_is_rejected(self):
        ingest = DynamoJeyukIngestor.__new__(DynamoJeyukIngestor)
        ingest.table = Table(); ingest.sqs = Mock(); ingest.queue_url = "test"
        ingest.ingest(sample(1))
        ingest.ingest(sample(1))
        self.assertEqual(ingest.table.revision, 1)
        with self.assertRaises(ValueError): ingest.ingest(sample(1, 191))
        self.assertEqual(ingest.table.revision, 1)
        self.assertEqual(ingest.sqs.send_message.call_count, 2)
        ingest.table.conflicts = 2
        with unittest.mock.patch("foodtech_collection_adapter.jeyuk_aws.time.sleep") as sleep:
            ingest.ingest(sample(2))
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(ingest.table.revision, 2)
        ingest.table.fail = True
        with self.assertRaises(Cancelled): ingest.ingest(sample(3))
        self.assertEqual(ingest.table.revision, 2)
        self.assertEqual(ingest.sqs.send_message.call_count, 3)
