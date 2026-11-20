import json
import unittest
from pathlib import Path

from foodtech_collection_adapter.opcua import validate_history

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
