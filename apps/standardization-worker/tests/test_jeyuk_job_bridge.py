import unittest
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from foodtech_contracts.jeyuk_process import RULE_VERSION
from foodtech_standardization_worker.jeyuk_job_bridge import DynamoJeyukJobBridge
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker


class TransactionCanceled(Exception):
    def __init__(self, reasons=None):
        self.response = {"CancellationReasons": reasons or []}


class Table:
    name = "test-cooking"

    def __init__(self):
        self.items = {}

    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item else {}


class Client:
    exceptions = SimpleNamespace(TransactionCanceledException=TransactionCanceled)

    def __init__(self, table):
        self.table = table
        self.decoder = TypeDeserializer()
        self.owner_locked = False

    def transact_write_items(self, *, TransactItems):
        writes = []
        for index, operation in enumerate(TransactItems):
            if 'ConditionCheck' in operation:
                if self.owner_locked:
                    reasons = [{'Code': 'None'} for _ in TransactItems]
                    reasons[index] = {'Code': 'ConditionalCheckFailed'}
                    raise TransactionCanceled(reasons)
                continue
            put = operation["Put"]
            item = {key: self.decoder.deserialize(value) for key, value in put["Item"].items()}
            key = (item["pk"], item["sk"])
            current = self.table.items.get(key)
            if put["ConditionExpression"] == "#version = :expected":
                expected = self.decoder.deserialize(put["ExpressionAttributeValues"][":expected"])
                allowed = current is not None and current["version"] == expected
            elif put["ConditionExpression"] == "revision = :prior":
                expected = self.decoder.deserialize(put["ExpressionAttributeValues"][":prior"])
                allowed = current is not None and current["revision"] == expected
            else:
                allowed = current is None
            if not allowed:
                raise TransactionCanceled()
            writes.append((key, item))
        for key, item in writes:
            self.table.items[key] = item


def decision(second: int, status: str) -> dict:
    return {"fixture_id": "jeyuk-small-process-v1", "second": second,
            "profile": "SMALL", "actual_load_kg": "1.8",
            "process_status": status, "process_rule_version": RULE_VERSION,
            "reason_codes": ["AGITATOR_FAILURE"] if status == "FAILED" else
                            ["AGITATOR_JAM_WARNING"] if status == "WARNING" else [],
            "surface_band": "NORMAL", "rpm_band": "LOW_DEVIATION" if second >= 5 else "NORMAL",
            "load_status": "NORMAL", "replayed": False}


class JeyukJobBridgeTests(unittest.TestCase):
    def setUp(self):
        self.table = Table()
        self.job_pk = "SCOPE#USER#one#JOB#jeyuk"
        self.stream_pk = "JEYUK#jeyuk-small-process-v1"
        self.table.items[(self.stream_pk, "BINDING")] = {"job_pk": self.job_pk}
        self.table.items[(self.job_pk, "META")] = {
            "owner_subject": "one", "job_id": "jeyuk", "menu": "JEYUK", "status": "RUNNING", "version": 1,
            "job_state_version": 1, "evaluation_blocked": True,
            "process_assessment_status": "INSUFFICIENT_DATA",
            "source_binding": {"source_id": "synthetic-rest",
                               "device_id": "mockprofile-110-a-001",
                               "batch_id": "jeyuk-small-process-v1"},
            "jeyuk_size_profile": "SMALL", "actual_load_kg": "1.8",
        }
        self.bridge = DynamoJeyukJobBridge.__new__(DynamoJeyukJobBridge)
        self.bridge.table = self.table
        self.bridge.client = Client(self.table)
        self.bridge.serializer = TypeSerializer()
        self.bridge.auth_table_name = 'auth-table'

    def test_legacy_assessment_and_job_are_not_relabelled(self):
        from copy import deepcopy
        before = deepcopy(self.table.items)
        for version in (None, "jeyuk-process-v1"):
            old = decision(1, "NORMAL")
            old["process_rule_version"] = version
            with self.assertRaisesRegex(ValueError, "version"):
                self.bridge.apply(old)
            self.assertEqual(self.table.items, before)
        self.table.items[(self.job_pk, "META")]["process_assessment"] = {
            "second": 1, "rule_version": "jeyuk-process-v1"}
        before = deepcopy(self.table.items)
        with self.assertRaisesRegex(ValueError, "version"):
            self.bridge.apply(decision(2, "NORMAL"))
        self.assertEqual(self.table.items, before)

    def test_matching_legacy_replay_preserves_event_without_new_writes(self):
        from copy import deepcopy
        self.bridge.apply(decision(1, "NORMAL"))
        key = (self.job_pk, "EVENT#JEYUK#000001")
        self.table.items[key]["rule_version"] = "jeyuk-process-v1"
        before = deepcopy(self.table.items)
        result = decision(1, "NORMAL")
        result["process_rule_version"] = "jeyuk-process-v1"
        self.assertEqual(self.bridge.apply(result), "REPLAYED")
        self.assertEqual(self.table.items, before)
        for field, value in (("process_rule_version", RULE_VERSION),
                             ("process_status", "FAILED"),
                             ("reason_codes", ["MOTOR_OVERLOAD_TRIP"])):
            changed = {**result, field: value}
            with self.assertRaisesRegex(ValueError, "replay"):
                self.bridge.apply(changed)
            self.assertEqual(self.table.items, before)
        self.table.items[key]["source_assessment_pk"] = "JEYUK#another-source"
        with self.assertRaisesRegex(ValueError, "replay"):
            self.bridge.apply(result)

    def test_conflicting_event_after_transaction_race_is_not_acknowledged(self):
        def lost_race(**_):
            self.table.items[(self.job_pk, "EVENT#JEYUK#000001")] = {
                "rule_version": "jeyuk-process-v1", "to_status": "NORMAL", "reason_codes": [],
                "source_assessment_pk": self.stream_pk, "source_assessment_sk": "ASSESSMENT#000001"}
            raise TransactionCanceled()
        self.bridge.client.transact_write_items = lost_race
        with self.assertRaisesRegex(ValueError, "replay"):
            self.bridge.apply(decision(1, "NORMAL"))
        self.assertEqual(self.table.items[(self.job_pk, "META")]["version"], 1)

    def test_controller_trip_blocks_evaluation_and_preserves_cause(self):
        result = decision(1, "FAILED")
        result["reason_codes"] = ["AGITATOR_FAILURE", "MOTOR_OVERLOAD_TRIP"]
        self.assertEqual(self.bridge.apply(result), "FAILED")
        job = self.table.items[(self.job_pk, "META")]
        self.assertEqual(job["status"], "FAILED")
        self.assertTrue(job["evaluation_blocked"])
        self.assertEqual(job["process_assessment"]["rule_version"], RULE_VERSION)
        self.assertIn("MOTOR_OVERLOAD_TRIP", job["last_transition_reason"])
        self.assertNotIn("EMERGENCY_STOPPED", job["last_transition_reason"])

    def test_locked_owner_does_not_write_and_requests_retry(self):
        self.bridge.client.owner_locked = True
        with self.assertRaisesRegex(RuntimeError, 'deletion-locked'):
            self.bridge.apply(decision(1, 'NORMAL'))
        self.assertEqual(self.table.items[(self.job_pk, 'META')]['version'], 1)
        self.assertNotIn((self.job_pk, 'EVENT#JEYUK#000001'), self.table.items)
        self.bridge.client.owner_locked = False
        self.assertEqual(self.bridge.apply(decision(1, 'NORMAL')), 'NORMAL')

    def test_locked_failure_does_not_acknowledge_sqs_message(self):
        deleted = []
        worker = DynamoStandardizationWorker.__new__(DynamoStandardizationWorker)
        worker.queue_url = 'queue'
        worker.sqs = SimpleNamespace(receive_message=lambda **_: {'Messages': [
            {'Body': '{}', 'ReceiptHandle': 'synthetic'}]}, delete_message=lambda **kw: deleted.append(kw))
        def process(_):
            raise RuntimeError('owner is suspended or deletion-locked; retry after reconciliation')
        worker.process = process
        with self.assertRaisesRegex(RuntimeError, 'deletion-locked'):
            worker.run_once()
        self.assertEqual(deleted, [])

    def test_warning_failure_and_replay_change_only_bound_job(self):
        for second in range(1, 31):
            status = "FAILED" if second == 30 else "WARNING" if second >= 5 else "NORMAL"
            self.assertEqual(self.bridge.apply(decision(second, status)), status)
        job = self.table.items[(self.job_pk, "META")]
        self.assertEqual((job["status"], job["job_state_version"], job["version"]),
                         ("FAILED", 2, 31))
        self.assertTrue(job["evaluation_blocked"])
        self.assertEqual(job["process_assessment"]["second"], 30)
        event = self.table.items[(self.job_pk, "EVENT#JEYUK#000030")]
        self.assertEqual(event["trigger"], "PROCESS_FAILURE_DETECTED")
        self.assertEqual(event["validation_change_reason"], "PROCESS_ANOMALY")
        self.assertEqual(self.bridge.apply(decision(30, "FAILED")), "REPLAYED")
        self.assertEqual(self.table.items[(self.job_pk, "META")]["version"], 31)
        self.assertEqual(self.table.items[("SCOPE#USER#one#JOBS", "HISTORY#REVISION")]["revision"], 3)
        self.assertEqual(self.bridge.apply(decision(30, "FAILED")), "REPLAYED")
        self.assertEqual(self.table.items[("SCOPE#USER#one#JOBS", "HISTORY#REVISION")]["revision"], 3)

    def test_unbound_and_out_of_order_are_not_applied(self):
        self.table.items.pop((self.stream_pk, "BINDING"))
        self.assertEqual(self.bridge.apply(decision(1, "NORMAL")), "UNBOUND")
        self.table.items[(self.stream_pk, "BINDING")] = {"job_pk": self.job_pk}
        with self.assertRaisesRegex(ValueError, "consecutive"):
            self.bridge.apply(decision(2, "NORMAL"))
        self.assertEqual(self.table.items[(self.job_pk, "META")]["version"], 1)
        self.assertEqual(self.bridge.apply({**decision(1, "NORMAL"), "actual_load_kg": "2.0"}),
                         "BINDING_MISMATCH")

    def test_history_conflict_retries_without_losing_concurrent_change(self):
        key = ("SCOPE#USER#one#JOBS", "HISTORY#REVISION")
        self.table.items[key] = {"pk": key[0], "sk": key[1], "revision": 5, "marker": "preserve"}
        original = self.bridge.client.transact_write_items
        calls = 0
        def concurrent(*, TransactItems):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.table.items[key]["revision"] = 6
                raise TransactionCanceled([{"Code": "None"}, {"Code": "None"},
                                           {"Code": "ConditionalCheckFailed"}])
            return original(TransactItems=TransactItems)
        self.bridge.client.transact_write_items = concurrent
        self.assertEqual(self.bridge.apply(decision(1, "NORMAL")), "NORMAL")
        self.assertEqual((calls, self.table.items[key]["revision"]), (2, 7))
        self.assertEqual(self.table.items[key]["marker"], "preserve")

    def test_auto_safety_proof_is_visible_but_does_not_complete_manual_validation(self):
        for second in range(1, 62):
            result = {**decision(second, "NORMAL"),
                      "auto_safety_status": "PASSED" if second == 61 else "PENDING",
                      "mixture_hold_seconds": second - 1,
                      "safety_passed_at_second": 61 if second == 61 else None}
            self.assertEqual(self.bridge.apply(result), "NORMAL")
        job = self.table.items[(self.job_pk, "META")]
        self.assertEqual((job["auto_safety_status"], job["mixture_hold_seconds"]), ("PASSED", 60))
        self.assertTrue(job["evaluation_blocked"])


if __name__ == "__main__":
    unittest.main()
