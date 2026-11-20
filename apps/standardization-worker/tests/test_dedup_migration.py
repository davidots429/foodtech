import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from foodtech_contracts import decode_record, dedup_key, legacy_dedup_key
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class ConditionalFailure(Exception): pass


class Table:
    def __init__(self):
        self.items = {}
        self.meta = SimpleNamespace(client=SimpleNamespace(exceptions=SimpleNamespace(ConditionalCheckFailedException=ConditionalFailure)))
    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item else {}
    def put_item(self, *, Item, ConditionExpression):
        key = (Item["pk"], Item["sk"])
        if key in self.items: raise ConditionalFailure()
        self.items[key] = Item


def dedup_pk(key):
    return "DEDUP#" + hashlib.sha256(key.encode()).hexdigest()


class DedupMigrationTests(unittest.TestCase):
    def setUp(self):
        self.a = json.loads((ROOT / "fixtures/mockprofile-100-a-001.json").read_text())["records"][0]
        self.b = json.loads((ROOT / "fixtures/mockprofile-300-a-001.json").read_text())["records"][0]
        for key in ("sourceId", "schemaVersion", "recordId"):
            self.b[key] = self.a[key]
        self.worker = DynamoStandardizationWorker.__new__(DynamoStandardizationWorker)
        self.worker.table = Table()

    def seed_legacy(self, record, event="old"):
        raw = json.dumps(record)
        evaluation = assess(event, raw)
        self.worker.table.items[(dedup_pk(legacy_dedup_key(record)), "CANONICAL")] = {
            "business_dedup_key": legacy_dedup_key(record), "payload_hash": evaluation.payload_hash,
            "first_ingestion_event_id": event}
        self.worker.table.items[("EVENT#" + event, "ASSESSMENT#v1")] = {"source_pk": "raw", "raw_sk": event}
        self.worker.table.items[("raw", event)] = {"raw": raw}
        return evaluation

    def test_new_key_includes_device_and_old_key_is_unchanged(self):
        self.assertEqual(legacy_dedup_key(self.a), legacy_dedup_key(self.b))
        self.assertNotEqual(dedup_key(self.a), dedup_key(self.b))
        self.assertEqual(decode_record(json.dumps(self.a))["deviceId"], self.a["deviceId"])

    def test_fallback_is_timezone_normalized_and_disjoint(self):
        fallback = {"sourceId": "s", "deviceId": "d", "sequenceNumber": 7,
                    "eventTime": "2026-01-01T09:00:00+09:00"}
        utc = {**fallback, "eventTime": "2026-01-01T00:00:00Z"}
        self.assertEqual(dedup_key(fallback), dedup_key(utc))
        self.assertNotEqual(dedup_key(fallback), dedup_key({**fallback, "deviceId": "other"}))
        self.assertNotEqual(dedup_key(fallback), dedup_key({**fallback, "recordId": "7", "schemaVersion": "1"}))
        with self.assertRaises(ValueError):
            dedup_key({**fallback, "sequenceNumber": True})
        with self.assertRaises(ValueError):
            dedup_key({**fallback, "eventTime": "2026-01-01T00:00:00"})

    def test_legacy_same_device_replays_and_conflicts_without_overwrite(self):
        self.seed_legacy(self.a)
        same = assess("new", json.dumps(self.a))
        self.assertEqual(self.worker._deduplicate(same), "DUPLICATE")
        changed = json.loads(json.dumps(self.a))
        changed["measurements"]["temperatureCelsius"] += 1
        self.assertEqual(self.worker._deduplicate(assess("other", json.dumps(changed))), "CONFLICT")
        self.assertEqual(self.worker.table.items[(dedup_pk(legacy_dedup_key(self.a)), "CANONICAL")]["first_ingestion_event_id"], "old")

    def test_legacy_other_device_is_not_a_conflict(self):
        self.seed_legacy(self.a)
        candidate = assess("new", json.dumps(self.b))
        self.assertEqual(self.worker._deduplicate(candidate), "UNIQUE")
        self.assertEqual(self.worker._deduplicate(candidate), "UNIQUE")
        self.assertEqual(self.worker._deduplicate(assess("new-2", json.dumps(self.b))), "DUPLICATE")

    def test_missing_or_tampered_legacy_owner_fails_closed(self):
        self.seed_legacy(self.a)
        del self.worker.table.items[("EVENT#old", "ASSESSMENT#v1")]
        with self.assertRaisesRegex(ValueError, "assessment is unavailable"):
            self.worker._deduplicate(assess("new", json.dumps(self.a)))
        self.seed_legacy(self.a)
        self.worker.table.items[("raw", "old")]["raw"] = json.dumps(self.b)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.worker._deduplicate(assess("new", json.dumps(self.a)))

    def test_incomplete_same_event_reservation_can_recover(self):
        self.seed_legacy(self.a)
        del self.worker.table.items[("EVENT#old", "ASSESSMENT#v1")]
        self.assertEqual(self.worker._deduplicate(assess("old", json.dumps(self.a))), "UNIQUE")
