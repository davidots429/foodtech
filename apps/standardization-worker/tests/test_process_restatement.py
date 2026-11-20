import unittest
from unittest.mock import AsyncMock, Mock, patch

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

    def test_observation_span_reports_exact_unobserved_edges_and_missing_bounds(self):
        job = {**self.job(), "actual_placed_at": "2026-11-14T00:00:00.250000Z",
               "actual_lifted_at": "2026-11-14T00:00:12.750000Z"}
        result = replay_process([self.sample("first", 1, 5), self.sample("last", 2, 10)], job)
        coverage = result["observation_coverage"]
        self.assertEqual((coverage["before_first_us"], coverage["after_last_us"]), (4750000, 2750000))
        self.assertEqual(coverage["sample_count"], 2)
        self.assertFalse(coverage["internal_gap_detected"])
        unknown = replay_process([self.sample("first", 1, 5)], self.job())["observation_coverage"]
        self.assertIsNone(unknown["before_first_us"])
        self.assertIsNone(unknown["after_last_us"])

    def test_observation_span_reports_internal_gap_independently_of_edges(self):
        job = {**self.job(), "actual_placed_at": "2026-11-14T00:00:00Z",
               "actual_lifted_at": "2026-11-14T00:00:10Z"}
        result = replay_process([self.sample("first", 1, 0), self.sample("last", 3, 10)], job)
        coverage = result["observation_coverage"]
        self.assertEqual((coverage["before_first_us"], coverage["after_last_us"]), (0, 0))
        self.assertTrue(coverage["internal_gap_detected"])

    def test_fractional_interval_over_five_seconds_is_a_gap(self):
        first, last = self.sample("first", 1, 0), self.sample("last", 2, 5)
        last["normalized"]["event_time"] = "2026-11-14T00:00:05.000001Z"
        coverage = replay_process([first, last], self.job())["observation_coverage"]
        self.assertTrue(coverage["internal_gap_detected"])

    def test_reversed_cooking_interval_is_rejected(self):
        job = {**self.job(), "actual_placed_at": "2026-11-14T00:00:10Z",
               "actual_lifted_at": "2026-11-14T00:00:00Z"}
        with self.assertRaisesRegex(ValueError, "reversed"):
            replay_process([self.sample("first", 1, 5)], job)

    def test_replay_preserves_unfilled_sequence_gap(self):
        result = replay_process([self.sample("first", 1, 0),
                                 self.sample("third", 3, 5),
                                 self.sample("fourth", 4, 10)], self.job())
        self.assertTrue(result["process_assessment"]["data_gap"])
        self.assertTrue(result["has_unfilled_gap"])
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
        cooking.get_item.side_effect = lambda **kwargs: (
            {"Item": {request["sk"]: request, "META": job}[kwargs["Key"]["sk"]]}
            if kwargs["Key"]["sk"] != "PROCESS#LATEST_RESTATED" else {})
        data = Mock(name="data")
        data.get_item.side_effect = lambda **kwargs: ({} if kwargs["Key"]["sk"] == "SOURCE#REVISION" else
            {"Item": {"EVENT#first": first, "EVENT#late": late,
                      "EVENT#after-terminal": after_terminal}[kwargs["Key"]["pk"]]})
        processor = DynamoProcessRestatement.__new__(DynamoProcessRestatement)
        processor.cooking, processor.data, processor.client = cooking, data, Mock()
        processor.serializer = TypeSerializer()
        processor._refresh_dependents = Mock()
        processor._query = lambda _pk, prefix: ([request] if prefix.startswith("RESTATEMENT") else
                                                   [{"source_assessment_pk": "EVENT#first"}])
        import hashlib
        source_pk = stream_binding_key(**self.binding)
        raws = [{"ingestion_event_id": event_id, "pk": source_pk, "provenance": "SYNTHETIC", "raw": event_id}
                for event_id in ("first", "late", "after-terminal")]
        receipt = {"sk": "COLLECTION#RECEIPT#1", "stream_pk": source_pk,
                   "receipt_version": "synthetic-rest-collection-receipt-v1", "provenance": "SYNTHETIC",
                   "profile_id": "mockprofile-100", "starting_cursor": 0, "ending_cursor": 3,
                   "record_count": 3, "raw_sha256": [hashlib.sha256(row["raw"].encode()).hexdigest() for row in raws]}
        processor._stream_sources = lambda _pk: raws
        processor._collection_receipts = lambda _pk: [receipt]
        self.assertEqual(processor.process("JOB#1", "late"), "DERIVED")
        operations = processor.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(operations), 5)
        self.assertEqual(operations[0]["ConditionCheck"]["Key"]["sk"]["S"], "META")
        self.assertEqual(operations[1]["Put"]["Item"]["status"]["S"], "DERIVED_ONLY")
        self.assertEqual(operations[1]["Put"]["Item"]["source_raw_count"]["N"], "3")
        self.assertEqual(operations[1]["Put"]["Item"]["observation_coverage"]["M"]["sample_count"]["N"], "3")
        self.assertTrue(operations[1]["Put"]["Item"]["collection_evidence"]["M"]["raw_snapshot_matches"]["BOOL"])
        original_key = operations[1]["Put"]["Item"]["sk"]["S"]
        self.assertEqual(len(operations[1]["Put"]["Item"]["included_ingestion_event_ids"]["L"]), 3)
        self.assertEqual(operations[2]["Put"]["Item"]["status"]["S"], "COMPLETED")
        self.assertEqual(operations[3]["Put"]["Item"]["sk"]["S"], "PROCESS#LATEST_RESTATED")
        self.assertEqual(operations[3]["Put"]["Item"]["version"]["N"], "1")
        self.assertEqual(operations[3]["Put"]["ConditionExpression"], "attribute_not_exists(pk)")
        self.assertEqual(operations[4]["ConditionCheck"]["ConditionExpression"], "attribute_not_exists(pk)")
        self.assertEqual(job["status"], "COMPLETED")
        processor._refresh_dependents.assert_called_once_with("JOB#1")
        assessment_lookup = {"EVENT#first": first, "EVENT#late": late, "EVENT#after-terminal": after_terminal}
        data.get_item.side_effect = lambda **kwargs: ({"Item": {"revision": 4}}
            if kwargs["Key"]["sk"] == "SOURCE#REVISION" else {"Item": assessment_lookup[kwargs["Key"]["pk"]]})
        self.assertEqual(processor.process("JOB#1", "late"), "DERIVED")
        operations = processor.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(operations[1]["Put"]["Item"]["source_revision"]["N"], "4")
        self.assertEqual(operations[4]["ConditionCheck"]["ExpressionAttributeValues"][":r"]["N"], "4")
        revisions = iter((4, 5))
        data.get_item.side_effect = lambda **kwargs: ({"Item": {"revision": next(revisions)}}
            if kwargs["Key"]["sk"] == "SOURCE#REVISION" else {"Item": assessment_lookup[kwargs["Key"]["pk"]]})
        processor.client.transact_write_items.reset_mock()
        self.assertEqual(processor.process("JOB#1", "late"), "SOURCE_CHANGED_RETRY")
        processor.client.transact_write_items.assert_not_called()
        data.get_item.side_effect = lambda **kwargs: ({} if kwargs["Key"]["sk"] == "SOURCE#REVISION" else
            {"Item": assessment_lookup[kwargs["Key"]["pk"]]})
        pointer = {"pk": "JOB#1", "sk": "PROCESS#LATEST_RESTATED", "version": 1}
        cooking.get_item.side_effect = lambda **kwargs: {"Item": {
            request["sk"]: request, "META": job,
            "PROCESS#LATEST_RESTATED": pointer}[kwargs["Key"]["sk"]]}
        self.assertEqual(processor.process("JOB#1", "late"), "DERIVED")
        next_pointer = processor.client.transact_write_items.call_args.kwargs["TransactItems"][3]["Put"]
        self.assertEqual(next_pointer["Item"]["version"]["N"], "2")
        self.assertEqual(next_pointer["ExpressionAttributeValues"][":previous"]["N"], "1")
        with patch("foodtech_standardization_worker.process_restatement.COVERAGE_VERSION", "future-version"):
            self.assertEqual(processor.process("JOB#1", "late"), "DERIVED")
        new_result = processor.client.transact_write_items.call_args.kwargs["TransactItems"][1]["Put"]["Item"]
        self.assertNotEqual(new_result["sk"]["S"], original_key)

    def test_completed_request_retries_aggregate_refresh(self):
        processor = DynamoProcessRestatement.__new__(DynamoProcessRestatement)
        processor.cooking = Mock()
        processor.cooking.get_item.return_value = {"Item": {"status": "COMPLETED"}}
        processor._refresh_dependents = Mock()
        self.assertEqual(processor.process("JOB#1", "late"), "ALREADY_COMPLETED")
        processor._refresh_dependents.assert_called_once_with("JOB#1")

    def test_customer_request_replays_current_source_without_trigger_event(self):
        job = self.job()
        sample = self.sample("first", 1, 0)
        source_pk = stream_binding_key(**self.binding)
        request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        request = {"pk": "JOB#1", "sk": f"RESTATEMENT#REQUEST#{request_id}", "status": "PENDING",
                   "reason": "CUSTOMER_REFRESH", "request_id": request_id, "source_stream_pk": source_pk}
        processor = DynamoProcessRestatement.__new__(DynamoProcessRestatement)
        processor.cooking, processor.data, processor.client = Mock(), Mock(), Mock()
        processor.cooking.name, processor.data.name = "cooking", "data"
        processor.serializer = TypeSerializer()
        processor._refresh_dependents = Mock()
        processor.cooking.get_item.side_effect = lambda **kwargs: (
            {} if kwargs["Key"]["sk"] == "PROCESS#LATEST_RESTATED" else
            {"Item": job if kwargs["Key"]["sk"] == "META" else request})
        processor.data.get_item.side_effect = lambda **kwargs: (
            {"Item": {"revision": 1}} if kwargs["Key"]["sk"] == "SOURCE#REVISION" else {"Item": sample})
        processor._query = lambda _pk, prefix: [request] if prefix.startswith("RESTATEMENT") else []
        processor._stream_sources = lambda _pk: [{"ingestion_event_id": "first"}]
        processor._collection_receipts = lambda _pk: []
        self.assertEqual(processor.process("JOB#1", request_id), "DERIVED")
        request_put = processor.client.transact_write_items.call_args.kwargs["TransactItems"][2]["Put"]["Item"]
        self.assertEqual(request_put["status"]["S"], "COMPLETED")

    def test_refresh_dependents_uses_business_aggregate_refresh(self):
        processor = DynamoProcessRestatement.__new__(DynamoProcessRestatement)
        processor.cooking = Mock()
        processor.cooking.name = "cooking-dev"
        job_pk = "SCOPE#USER#customer-1#JOB#aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        refresh = AsyncMock(return_value=1)
        with patch("foodtech_business_api.assessment_aggregate.refresh_dependents", refresh), \
             patch("foodtech_business_api.cooking_store.DynamoCookingStore") as store:
            processor._refresh_dependents(job_pk)
        refresh.assert_awaited_once_with(store.return_value, job_pk)

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
        processor._collection_receipts = lambda _pk: []
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            processor.process("JOB#1", "late")
        client.transact_write_items.assert_not_called()

    def test_followup_detection_compares_immutable_result_sources(self):
        processor = DynamoProcessRestatement.__new__(DynamoProcessRestatement)
        processor.cooking = Mock()
        pointer = {"result_sk": "RESTATEMENT#RESULT#prior"}
        result = {"status": "DERIVED_ONLY",
                  "source_assessment_keys": ["EVENT#first/ASSESSMENT#v1"]}
        processor.cooking.get_item.side_effect = lambda **kwargs: (
            {"Item": pointer} if kwargs["Key"]["sk"] == "PROCESS#LATEST_RESTATED"
            else {"Item": result})
        self.assertFalse(processor.has_new_source("JOB#1", self.sample("first", 1, 0)))
        self.assertTrue(processor.has_new_source("JOB#1", self.sample("followup", 3, 10)))


if __name__ == "__main__":
    unittest.main()
