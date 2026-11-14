import unittest
from unittest.mock import Mock

from boto3.dynamodb.types import TypeSerializer
from foodtech_contracts import stream_binding_key

from foodtech_standardization_worker.process_restatement import (
    DynamoProcessRestatement, replay_process,
)


class ProcessRestatementTests(unittest.TestCase):
    binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
               "batch_id": "batch-1"}

    def sample(self, event_id, sequence, seconds, temperature=170, late=False):
        return {"pk": f"EVENT#{event_id}", "sk": "ASSESSMENT#v1",
                "source_pk": stream_binding_key(**self.binding),
                "ingestion_event_id": event_id, "dedup_status": "UNIQUE",
                "quality_status": "WARNING" if late else "VALID",
                "finding_codes": ["LATE_AFTER_GAP"] if late else [],
                "normalized": {**self.binding, "sequence_number": sequence,
                               "event_time": f"2026-11-14T00:00:{seconds:02d}Z",
                               "device_status": "RUNNING",
                               "CHAMBER_TEMPERATURE": {"value": str(temperature), "unit": "°C"}}}

    def job(self):
        return {"pk": "JOB#1", "sk": "META", "job_id": "1", "version": 7,
                "status": "COMPLETED", "source_binding": self.binding,
                "process_thickness_status": "NORMAL", "process_time_status": "NORMAL"}

    def test_replay_fills_gap_from_event_time_and_keeps_original_job_untouched(self):
        job = self.job()
        first = self.sample("first", 1, 0)
        late = self.sample("late", 2, 5, late=True)
        third = self.sample("third", 3, 10)
        result = replay_process([third, late, first], job)
        self.assertEqual(result["included_ingestion_event_ids"], ["first", "late", "third"])
        self.assertEqual(result["process_assessment_status"], "NORMAL")
        self.assertFalse(result["process_assessment"]["data_gap"])
        self.assertNotIn("process_assessment", job)

    def test_replay_rejects_wrong_binding(self):
        item = self.sample("other", 2, 5, late=True)
        item["normalized"]["batch_id"] = "other"
        with self.assertRaises(ValueError):
            replay_process([item], self.job())

    def test_replay_rejects_late_sample_after_lifting(self):
        job = {**self.job(), "actual_placed_at": "2026-11-14T00:00:00Z",
               "actual_lifted_at": "2026-11-14T00:00:04Z"}
        with self.assertRaisesRegex(ValueError, "outside the cooking interval"):
            replay_process([self.sample("late", 2, 5, late=True)], job)

    def test_replay_preserves_unfilled_sequence_gap(self):
        result = replay_process([self.sample("first", 1, 0),
                                 self.sample("third", 3, 5)], self.job())
        self.assertTrue(result["process_assessment"]["data_gap"])
        self.assertEqual(result["process_assessment_status"], "WARNING")

    def test_process_writes_immutable_result_and_request_without_meta_update(self):
        job = self.job()
        first = self.sample("first", 1, 0)
        late = self.sample("late", 2, 5, late=True)
        after_terminal = self.sample("after-terminal", 3, 10)
        request = {"pk": "JOB#1", "sk": "RESTATEMENT#REQUEST#late", "status": "PENDING",
                   "source_assessment_pk": "EVENT#late", "source_assessment_sk": "ASSESSMENT#v1",
                   "source_stream_pk": stream_binding_key(**self.binding)}
        cooking = Mock(name="cooking")
        cooking.name = "cooking-dev"
        cooking.get_item.side_effect = lambda **kwargs: {"Item": {
            request["sk"]: request, "META": job}[kwargs["Key"]["sk"]]}
        data = Mock(name="data")
        data.get_item.side_effect = lambda **kwargs: {"Item": {
            "EVENT#first": first, "EVENT#late": late,
            "EVENT#after-terminal": after_terminal}[kwargs["Key"]["pk"]]}
        processor = DynamoProcessRestatement.__new__(DynamoProcessRestatement)
        processor.cooking, processor.data, processor.client = cooking, data, Mock()
        processor.serializer = TypeSerializer()
        processor._query = lambda _pk, prefix: ([request] if prefix.startswith("RESTATEMENT") else
                                                   [{"source_assessment_pk": "EVENT#first"}])
        processor._stream_sources = lambda _pk: [
            {"ingestion_event_id": event_id} for event_id in ("first", "late", "after-terminal")]
        self.assertEqual(processor.process("JOB#1", "late"), "DERIVED")
        operations = processor.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(operations), 3)
        self.assertEqual(operations[0]["ConditionCheck"]["Key"]["sk"]["S"], "META")
        self.assertEqual(operations[1]["Put"]["Item"]["status"]["S"], "DERIVED_ONLY")
        self.assertEqual(operations[1]["Put"]["Item"]["source_raw_count"]["N"], "3")
        self.assertEqual(len(operations[1]["Put"]["Item"]["included_ingestion_event_ids"]["L"]), 3)
        self.assertEqual(operations[2]["Put"]["Item"]["status"]["S"], "COMPLETED")
        self.assertEqual(job["status"], "COMPLETED")

    def test_process_waits_for_unstandardized_raw_sample(self):
        job = self.job()
        late = self.sample("late", 2, 5, late=True)
        request = {"pk": "JOB#1", "sk": "RESTATEMENT#REQUEST#late", "status": "PENDING",
                   "source_assessment_pk": "EVENT#late", "source_stream_pk": late["source_pk"]}
        cooking, data, client = Mock(), Mock(), Mock()
        cooking.get_item.side_effect = lambda **kwargs: {"Item": {
            request["sk"]: request, "META": job}[kwargs["Key"]["sk"]]}
        data.get_item.side_effect = lambda **kwargs: (
            {"Item": late} if kwargs["Key"]["pk"] == late["pk"] else {})
        processor = DynamoProcessRestatement.__new__(DynamoProcessRestatement)
        processor.cooking, processor.data, processor.client = cooking, data, client
        processor._query = lambda _pk, prefix: [request] if prefix.startswith("RESTATEMENT") else []
        processor._stream_sources = lambda _pk: [
            {"ingestion_event_id": "late"}, {"ingestion_event_id": "unstandardized"}]
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            processor.process("JOB#1", "late")
        client.transact_write_items.assert_not_called()


if __name__ == "__main__":
    unittest.main()
