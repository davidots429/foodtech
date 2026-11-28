import json
import unittest
from unittest.mock import Mock
from uuid import uuid4

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from foodtech_contracts import stream_key
from foodtech_contracts.collection_evidence import CompletionTracker
from foodtech_contracts.tonkatsu_source_quality import QUALITY_PREFIX, verify_source_quality
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker


class FryerSourceQualityTests(unittest.TestCase):
    def setUp(self):
        record = {
            "schemaVersion": "1.0", "sourceId": "synthetic-rest", "deviceId": "mockprofile-100-a-001",
            "batchId": "batch-a-fryer", "recordId": "rec-a-fryer", "sequenceNumber": 1,
            "eventTime": "2026-11-28T00:00:05Z", "deviceStatus": "running",
            "measurements": {"temperatureCelsius": 170, "speedRpm": 0, "elapsedSeconds": 5},
            "extensions": {"tonkatsuSamples": {"schemaVersion": "tonkatsu-controller-samples-v1"}},
        }
        self.stream = stream_key(record)
        self.event = str(uuid4())
        self.raw = {
            "pk": self.stream, "sk": f"RAW#{self.event}", "ingestion_event_id": self.event,
            "provenance": "SYNTHETIC", "raw": json.dumps(record),
            "source_id": record["sourceId"], "device_id": record["deviceId"],
        }
        tracker = CompletionTracker(0, "mockprofile-100", record["deviceId"])
        tracker.record(self.raw["raw"], record)
        self.receipt = {"pk": self.stream, **tracker.finish(next_cursor=1, complete=True, completion_explicit=True)}
        self.worker = DynamoStandardizationWorker.__new__(DynamoStandardizationWorker)
        self.worker.table = Mock()
        self.worker.table.name = "test"
        self.worker.ddb = Mock()
        self.worker.serializer = TypeSerializer()
        self.worker._deduplicate = Mock(return_value="UNIQUE")

    def process(self, **changes):
        self.worker.table.get_item.side_effect = [{}, {"Item": self.raw}]
        reference = {
            "pk": self.stream, "raw_sk": self.raw["sk"], "ingestion_event_id": self.event,
            "sequence_status": "IN_ORDER",
        }
        self.worker.process({**reference, **changes})
        decoder = TypeDeserializer()
        actions = self.worker.ddb.transact_write_items.call_args.kwargs["TransactItems"]
        return [
            {key: decoder.deserialize(value) for key, value in action["Put"]["Item"].items()}
            for action in actions
        ]

    def test_worker_commits_assessment_and_scoped_projection_together(self):
        assessment, projection = self.process()
        self.assertEqual(assessment["sk"], "ASSESSMENT#v1")
        self.assertEqual(projection["sk"], QUALITY_PREFIX + self.event)
        self.assertNotIn("raw", projection)
        verify_source_quality(self.stream, [self.raw], self.receipt, [projection])

    def test_missing_foreign_and_warning_projections_cannot_satisfy_receipt(self):
        *_, warning = self.process(gap_before=[1])
        self.assertEqual(warning["quality_status"], "WARNING")
        for projections in ([], [warning], [{**warning, "quality_status": "VALID", "finding_codes": [], "pk": "foreign"}]):
            with self.subTest(projections=projections):
                with self.assertRaisesRegex(ValueError, "quality evidence unavailable"):
                    verify_source_quality(self.stream, [self.raw], self.receipt, projections)

    def test_ordinary_rest_records_keep_existing_storage_shape(self):
        record = json.loads(self.raw["raw"])
        record.pop("extensions")
        self.raw["raw"] = json.dumps(record)
        [assessment] = self.process()
        self.assertEqual(assessment["quality_status"], "VALID")

    def test_existing_delivery_replay_does_not_rewrite_projection(self):
        self.worker.table.get_item.return_value = {"Item": {"quality_status": "VALID", "dedup_status": "UNIQUE"}}
        result = self.worker.process({"ingestion_event_id": self.event})
        self.assertTrue(result["replayed"])
        self.worker.ddb.transact_write_items.assert_not_called()
