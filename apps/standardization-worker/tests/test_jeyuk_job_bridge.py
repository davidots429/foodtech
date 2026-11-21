import unittest
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from foodtech_standardization_worker.jeyuk_job_bridge import DynamoJeyukJobBridge


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

    def transact_write_items(self, *, TransactItems):
        writes = []
        for operation in TransactItems:
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
            "process_status": status,
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
        self.table.items[key] = {"pk": key[0], "sk": key[1], "revision": 5}
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
