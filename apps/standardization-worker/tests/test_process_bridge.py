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
            values = {key: self.deserializer.deserialize(value) for key, value in write["Put"]["Item"].items()}
            self.table.items[(values["pk"], values["sk"])] = values


class ProcessBridgeTests(unittest.TestCase):
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
        self.assertEqual(bridge.apply(assessment), "REPLAYED")
        self.assertEqual(bridge.client.calls, 5)

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
        self.assertEqual(bridge.client.calls, 0)
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
