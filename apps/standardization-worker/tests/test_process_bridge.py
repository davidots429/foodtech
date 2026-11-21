import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from foodtech_contracts import stream_binding_key
from foodtech_standardization_worker.process_bridge import DynamoProcessBridge
from foodtech_standardization_worker.process import assess


class FakeTable:
    name = "test-cooking"

    def __init__(self, items):
        self.items = items
        self.meta = SimpleNamespace(client=SimpleNamespace(exceptions=SimpleNamespace(
            ConditionalCheckFailedException=ConditionalWriteFailed)))

    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item.copy()} if item else {}

    def put_item(self, *, Item, ConditionExpression):
        assert ConditionExpression is not None
        key = (Item["pk"], Item["sk"])
        if key in self.items:
            raise ConditionalWriteFailed
        self.items[key] = Item.copy()


class ConditionalWriteFailed(Exception):
    pass


class FakeClient:
    def __init__(self, table):
        self.table = table
        self.deserializer = TypeDeserializer()
        self.calls = 0
        self.exceptions = SimpleNamespace(TransactionCanceledException=RuntimeError)

    def transact_write_items(self, *, TransactItems):
        self.calls += 1
        for write in TransactItems:
            if "Update" in write:
                key = {name: self.deserializer.deserialize(value) for name, value in write["Update"]["Key"].items()}
                item_key = (key["pk"], key["sk"])
                previous = self.table.items.get(item_key, {})
                self.table.items[item_key] = {**previous, **key, "revision": previous.get("revision", 0) + 1}
                continue
            values = {key: self.deserializer.deserialize(value) for key, value in write["Put"]["Item"].items()}
            self.table.items[(values["pk"], values["sk"])] = values


class ProcessBridgeTests(unittest.TestCase):
    def test_history_contention_retries_from_new_revision(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001", "batch_id": "race"}
        stream_pk = stream_binding_key(**binding)
        job_pk = "SCOPE#USER#customer-1#JOB#race"
        history_key = ("SCOPE#USER#customer-1#JOBS", "HISTORY#REVISION")
        table = FakeTable({(stream_pk, "BINDING"): {"job_pk": job_pk},
            (job_pk, "META"): {"pk": job_pk, "sk": "META", "source_binding": binding,
                "status": "RUNNING", "version": 1, "process_thickness_status": "IN_RANGE",
                "process_time_status": "NORMAL", "process_assessment_status": "INSUFFICIENT_DATA"},
            history_key: {"pk": history_key[0], "sk": history_key[1], "revision": 1}})

        class Cancelled(Exception):
            response = {"CancellationReasons": [{"Code": "None"}, {"Code": "None"},
                                                  {"Code": "ConditionalCheckFailed"}]}

        class ContendedClient(FakeClient):
            def __init__(self, table):
                super().__init__(table)
                self.exceptions.TransactionCanceledException = Cancelled

            def transact_write_items(self, *, TransactItems):
                if not self.calls:
                    self.calls += 1
                    self.table.items[history_key]["revision"] = 2
                    raise Cancelled()
                super().transact_write_items(TransactItems=TransactItems)

        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table, bridge.client, bridge.serializer = table, ContendedClient(table), TypeSerializer()
        assessment = {"pk": "EVENT#race", "ingestion_event_id": "race", "dedup_status": "UNIQUE",
            "quality_status": "VALID", "finding_codes": [], "normalized": {**binding,
                "device_status": "RUNNING", "event_time": "2026-11-02T00:00:00Z",
                "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}}
        bridge.apply(assessment)
        self.assertEqual(bridge.client.calls, 2)
        self.assertEqual(table.items[history_key]["revision"], 3)

    def test_gap_remains_a_warning_after_subsequent_normal_samples(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001", "batch_id": "gap"}
        stream_pk = stream_binding_key(**binding)
        job_pk = "SCOPE#USER#customer-1#JOB#gap"
        table = FakeTable({
            (stream_pk, "BINDING"): {"job_pk": job_pk},
            (job_pk, "META"): {"pk": job_pk, "sk": "META", "source_binding": binding,
                "status": "RUNNING", "version": 1, "job_state_version": 1,
                "process_thickness_status": "IN_RANGE", "process_time_status": "NORMAL",
                "process_assessment_status": "INSUFFICIENT_DATA"},
        })
        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table, bridge.client, bridge.serializer = table, FakeClient(table), TypeSerializer()
        for index, seconds in enumerate((0, 10, 15), 1):
            assessment = {"pk": f"EVENT#{index}", "ingestion_event_id": str(index),
                "dedup_status": "UNIQUE", "quality_status": "VALID", "finding_codes": [],
                "normalized": {**binding, "device_status": "RUNNING",
                    "event_time": f"2026-11-02T00:00:{seconds:02d}Z",
                    "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}}
            bridge.apply(assessment)
        meta = table.items[(job_pk, "META")]
        self.assertTrue(meta["process_assessment"]["data_gap"])
        self.assertEqual(meta["process_assessment_status"], "WARNING")
        history = table.items[("SCOPE#USER#customer-1#JOBS", "HISTORY#REVISION")]
        self.assertGreaterEqual(history["revision"], 2)

    def test_legacy_gap_history_remains_unverified(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001", "batch_id": "legacy"}
        stream_pk = stream_binding_key(**binding)
        job_pk = "SCOPE#USER#customer-1#JOB#legacy"
        table = FakeTable({(stream_pk, "BINDING"): {"job_pk": job_pk},
            (job_pk, "META"): {"pk": job_pk, "sk": "META", "source_binding": binding,
                "status": "RUNNING", "version": 1, "process_thickness_status": "IN_RANGE",
                "process_time_status": "NORMAL", "process_assessment_status": "NORMAL",
                "process_assessment": {"rule_version": "tonkatsu-temperature-v1", "status": "NORMAL",
                    "data_gap": False, "last_event_time": "2026-11-02T00:00:00Z"}}})
        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table, bridge.client, bridge.serializer = table, FakeClient(table), TypeSerializer()
        bridge.apply({"pk": "EVENT#next", "ingestion_event_id": "next", "dedup_status": "UNIQUE",
            "quality_status": "VALID", "finding_codes": [], "normalized": {**binding,
                "device_status": "RUNNING", "event_time": "2026-11-02T00:00:05Z",
                "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}})
        meta = table.items[(job_pk, "META")]
        self.assertFalse(meta["process_assessment"]["gap_history_verified"])
        self.assertEqual(meta["process_assessment_status"], "WARNING")

    def test_standardized_samples_fail_only_bound_running_job_and_replay_once(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
                   "batch_id": "batch-a-process-high-001"}
        stream_pk = stream_binding_key(**binding)
        job_pk = "SCOPE#USER#customer-1#JOB#job-1"
        items = {
            (stream_pk, "BINDING"): {"pk": stream_pk, "sk": "BINDING", "job_pk": job_pk},
            (job_pk, "META"): {"pk": job_pk, "sk": "META", "job_id": "job-1",
                                "source_binding": binding, "status": "RUNNING", "version": 1,
                                "job_state_version": 2, "evaluation_blocked": True,
                                "process_assessment_status": "INSUFFICIENT_DATA"},
        }
        table = FakeTable(items)
        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table = table
        bridge.client = FakeClient(table)
        bridge.serializer = TypeSerializer()
        fixture_path = Path(__file__).resolve().parents[3] / "fixtures/tonkatsu-process-high-v1.json"
        fixture = json.loads(fixture_path.read_text())
        for index, record in enumerate(fixture["records"], 1):
            event_id = f"event-{index}"
            standardized = assess(event_id, json.dumps(record))
            assessment = {"pk": f"EVENT#{event_id}", "ingestion_event_id": event_id,
                          "dedup_status": "UNIQUE", "quality_status": standardized.quality_status,
                          "finding_codes": [finding.code for finding in standardized.findings],
                          "normalized": standardized.normalized}
            result = bridge.apply(assessment)
            self.assertEqual(result, "FAILED" if index == 5 else "WARNING")
        self.assertEqual(table.items[(job_pk, "META")]["status"], "FAILED")
        self.assertEqual(table.items[(job_pk, "META")]["last_transition_event"], "PROCESS_FAILURE_DETECTED")
        self.assertEqual(table.items[(job_pk, "EVENT#PROCESS#event-5")]["source_assessment_pk"], "EVENT#event-5")
        self.assertEqual(table.items[(job_pk, "EVENT#PROCESS#event-5")]["validation_change_reason"], "PROCESS_ANOMALY")
        history_key = ("SCOPE#USER#customer-1#JOBS", "HISTORY#REVISION")
        revision = table.items[history_key]["revision"]
        self.assertEqual(bridge.apply(assessment), "REPLAYED")
        self.assertEqual(bridge.client.calls, 5)
        self.assertEqual(table.items[history_key]["revision"], revision)

    def test_unbound_event_has_no_job_effect(self):
        table = FakeTable({})
        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table = table
        bridge.client = FakeClient(table)
        bridge.serializer = TypeSerializer()
        result = bridge.apply({"pk": "EVENT#one", "ingestion_event_id": "one",
                               "dedup_status": "UNIQUE", "quality_status": "VALID",
                               "normalized": {"source_id": "synthetic-rest",
                                              "device_id": "mockprofile-100-a-001", "batch_id": "unknown",
                                              "device_status": "RUNNING"}})
        self.assertEqual(result, "UNBOUND")
        self.assertEqual(bridge.client.calls, 0)

    def test_safety_hold_does_not_accept_process_updates(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
                   "batch_id": "batch-a-safety-hold"}
        stream_pk = stream_binding_key(**binding)
        job_pk = "SCOPE#USER#customer-1#JOB#held"
        table = FakeTable({
            (stream_pk, "BINDING"): {"pk": stream_pk, "sk": "BINDING", "job_pk": job_pk},
            (job_pk, "META"): {"pk": job_pk, "sk": "META", "source_binding": binding,
                                "status": "SAFETY_HOLD", "version": 4,
                                "process_assessment_status": "INSUFFICIENT_DATA"},
        })
        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table, bridge.client, bridge.serializer = table, FakeClient(table), TypeSerializer()
        result = bridge.apply({
            "pk": "EVENT#held", "ingestion_event_id": "held", "dedup_status": "UNIQUE",
            "quality_status": "VALID", "normalized": {**binding, "device_status": "RUNNING"},
        })
        self.assertEqual(result, "JOB_NOT_ACTIVE")
        self.assertEqual(bridge.client.calls, 0)
        self.assertEqual(table.items[(job_pk, "META")]["status"], "SAFETY_HOLD")

    def test_normal_temperature_does_not_clear_thickness_warning(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
                   "batch_id": "batch-a-thin-001"}
        stream_pk = stream_binding_key(**binding)
        job_pk = "SCOPE#USER#customer-1#JOB#thin"
        table = FakeTable({
            (stream_pk, "BINDING"): {"pk": stream_pk, "sk": "BINDING", "job_pk": job_pk},
            (job_pk, "META"): {"pk": job_pk, "sk": "META", "source_binding": binding,
                                "status": "RUNNING", "version": 1, "job_state_version": 2,
                                "process_thickness_status": "BELOW_RANGE",
                                "process_time_status": "NOT_MEASURED",
                                "process_assessment_status": "WARNING"},
        })
        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table, bridge.client, bridge.serializer = table, FakeClient(table), TypeSerializer()
        result = bridge.apply({
            "pk": "EVENT#normal", "ingestion_event_id": "normal", "dedup_status": "UNIQUE",
            "quality_status": "VALID", "finding_codes": [],
            "normalized": {**binding, "device_status": "RUNNING",
                           "event_time": "2026-11-02T00:00:00Z",
                           "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}},
        })
        self.assertEqual(result, "WARNING")
        self.assertEqual(table.items[(job_pk, "META")]["process_assessment_status"], "WARNING")

    def test_late_event_creates_one_pending_restatement_without_changing_current_job(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
                   "batch_id": "batch-a-late"}
        stream_pk = stream_binding_key(**binding)
        job_pk = "SCOPE#USER#customer-1#JOB#job-late"
        meta = {"pk": job_pk, "sk": "META", "job_id": "job-late", "source_binding": binding,
                "status": "COMPLETED", "version": 8,
                "process_assessment_status": "WARNING", "process_temperature_status": "NORMAL"}
        table = FakeTable({(stream_pk, "BINDING"): {"job_pk": job_pk}, (job_pk, "META"): meta})
        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table, bridge.client, bridge.serializer = table, FakeClient(table), TypeSerializer()
        late = {"pk": "EVENT#late-1", "sk": "ASSESSMENT#v1", "source_pk": stream_pk,
                "ingestion_event_id": "late-1", "dedup_status": "UNIQUE",
                "quality_status": "WARNING", "finding_codes": ["LATE_AFTER_GAP"],
                "normalized": {**binding, "device_status": "RUNNING", "sequence_number": 2,
                               "event_time": "2026-11-02T00:00:05Z",
                               "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}}
        self.assertEqual(bridge.apply(late), "RESTATEMENT_PENDING")
        self.assertEqual(bridge.apply(late), "RESTATEMENT_ALREADY_PENDING")
        request = table.items[(job_pk, "RESTATEMENT#REQUEST#late-1")]
        self.assertEqual(request["status"], "PENDING")
        self.assertEqual(request["source_assessment_pk"], "EVENT#late-1")
        self.assertEqual(request["job_version_at_detection"], 8)
        self.assertEqual(table.items[(job_pk, "META")], meta)
        self.assertEqual(bridge.client.calls, 1)
        self.assertEqual(table.items[("SCOPE#USER#customer-1#JOBS", "HISTORY#REVISION")]["revision"], 1)
        bridge.restatement = SimpleNamespace(process=lambda pk, event: "DERIVED")
        self.assertEqual(bridge.apply(late), "RESTATEMENT_DERIVED")
        bridge.restatement = SimpleNamespace(process=lambda pk, event: "STALE_RETRY")
        with self.assertRaisesRegex(RuntimeError, "must be retried"):
            bridge.apply(late)

    def test_normal_sample_after_terminal_refreshes_existing_derivation_once(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
                   "batch_id": "batch-a-followup"}
        stream_pk = stream_binding_key(**binding)
        job_pk = "SCOPE#USER#customer-1#JOB#followup"
        meta = {"pk": job_pk, "sk": "META", "job_id": "followup", "source_binding": binding,
                "status": "COMPLETED", "version": 8, "process_assessment_status": "WARNING"}
        table = FakeTable({(stream_pk, "BINDING"): {"job_pk": job_pk}, (job_pk, "META"): meta})
        bridge = DynamoProcessBridge.__new__(DynamoProcessBridge)
        bridge.table, bridge.client, bridge.serializer = table, FakeClient(table), TypeSerializer()
        calls = []
        def derive(pk, event):
            calls.append((pk, event))
            return "STALE_RETRY" if len(calls) == 1 else "DERIVED"
        bridge.restatement = SimpleNamespace(
            has_new_source=lambda pk, item: len(calls) == 0,
            process=derive)
        sample = {"pk": "EVENT#followup-1", "sk": "ASSESSMENT#v1", "source_pk": stream_pk,
                  "ingestion_event_id": "followup-1", "dedup_status": "UNIQUE",
                  "quality_status": "VALID", "finding_codes": [],
                  "normalized": {**binding, "device_status": "RUNNING", "sequence_number": 3,
                                 "event_time": "2026-11-02T00:00:10Z"}}
        with self.assertRaisesRegex(RuntimeError, "must be retried"):
            bridge.apply(sample)
        self.assertEqual(bridge.apply(sample), "RESTATEMENT_DERIVED")
        request = table.items[(job_pk, "RESTATEMENT#REQUEST#followup-1")]
        self.assertEqual(request["reason"], "FOLLOWUP_SAMPLE")
        self.assertEqual(len(calls), 2)
        self.assertEqual(table.items[(job_pk, "META")], meta)


if __name__ == "__main__":
    unittest.main()
