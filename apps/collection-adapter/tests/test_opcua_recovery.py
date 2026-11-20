import json
import unittest
from pathlib import Path
from unittest.mock import patch

from foodtech_collection_adapter.opcua import validate_history, validate_or_quarantine
from foodtech_collection_adapter import opcua_json

ROOT = Path(__file__).resolve().parents[3]


class OpcUaRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((ROOT / "fixtures/mockprofile-203-a-001.json").read_text())
        self.records = [json.dumps(record) for record in fixture["records"]]
        self.device = fixture["instanceId"]

    def test_replay_is_valid_but_rewritten_or_missing_sequence_is_rejected(self):
        parsed, batch = validate_history(json.dumps(self.records[:2]), self.device, {}, None)
        seen = {number: digest for number, _raw, _record, digest in parsed}
        replay, _ = validate_history(json.dumps(self.records), self.device, seen, batch)
        self.assertEqual([item[0] for item in replay if item[0] not in seen], [3])
        changed = json.loads(self.records[0]); changed["measurements"]["temperatureCelsius"] = 1
        with self.assertRaisesRegex(ValueError, "rewrote"):
            validate_history(json.dumps([json.dumps(changed), *self.records[1:]]), self.device, seen, batch)
        with self.assertRaisesRegex(ValueError, "sequence"):
            validate_history(json.dumps([self.records[0], self.records[2]]), self.device, {}, None)

    def test_foreign_batch_is_rejected(self):
        changed = json.loads(self.records[1]); changed["batchId"] = "batch-a-foreign"
        with self.assertRaisesRegex(ValueError, "changed batch"):
            validate_history(json.dumps([self.records[0], json.dumps(changed)]), self.device, {}, None)

    def test_invalid_history_is_preserved_then_rejected_without_mutating_seen(self):
        calls = []
        class Sink:
            def preserve_opcua_history(self, payload, **kwargs):
                calls.append((payload, kwargs))
        seen = {}
        invalid = json.dumps([self.records[0], "{broken"])
        for profile, device, source in (("mockprofile-203", self.device, "synthetic-opcua"),
                                        ("mockprofile-200", "mockprofile-200-a-001", "synthetic-opcua-json")):
            with self.assertRaises(ValueError):
                validate_or_quarantine(invalid, device, seen, None, source_id=source,
                                       profile_id=profile, quarantine=Sink())
            self.assertEqual(calls[-1], (invalid.encode(), {"instance_id": device, "profile_id": profile}))
            self.assertEqual(seen, {})
        class FailedSink:
            def preserve_opcua_history(self, *args, **kwargs):
                raise RuntimeError("storage unavailable")
        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            validate_or_quarantine(invalid, self.device, seen, None, source_id="synthetic-opcua",
                                   profile_id="mockprofile-203", quarantine=FailedSink())
        self.assertEqual(seen, {})

    def test_json_collection_stops_before_ingesting_any_part_of_invalid_history(self):
        fixture = json.loads((ROOT / "fixtures/mockprofile-200-a-001.json").read_text())
        values = [json.dumps(fixture["records"][0]), "{broken"]
        calls = []
        class Client:
            def __init__(self, *args): pass
            def get(self, path): return {"nodes": ["DeviceId", "AvailableSnapshots"]}
            def read(self, node): return fixture["instanceId"] if node == "DeviceId" else values
            def close(self): calls.append("closed")
        class Ingestor:
            def ingest(self, raw): calls.append("ingested")
            def drain(self, stream): calls.append("drained")
        class Sink:
            def preserve_opcua_history(self, raw, **kwargs): calls.append("preserved")
        with patch.object(opcua_json, "UaJsonClient", Client):
            with self.assertRaises(ValueError):
                opcua_json.collect("url", "id", "secret", "ca", Ingestor(), count=2,
                                   timeout_seconds=1, quarantine=Sink())
        self.assertEqual(calls, ["preserved", "closed"])
