import json
import unittest
from pathlib import Path
from jsonschema import ValidationError

from foodtech_collection_adapter.ingestion_quarantine import failure
from foodtech_contracts import canonical_record, decode_record, dedup_key
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class VendorCTests(unittest.TestCase):
    def setUp(self):
        self.records = json.loads((ROOT / "fixtures/mockprofile-100-c-001.json").read_text())["records"]
        self.record = self.records[0]

    def test_mixed_source_preserves_level_offset_and_boolean(self):
        raw = json.dumps(self.record)
        self.assertIsNone(failure(raw))
        record = decode_record(raw)
        canonical = canonical_record(record)
        self.assertEqual(canonical["event_time"], "2026-11-01T00:00:00Z")
        self.assertEqual(canonical["speed_level"], 6)
        self.assertNotIn("speed_rpm", canonical)
        self.assertNotIn("output_percent", canonical)
        self.assertFalse(canonical["extensions"]["alarm"])
        self.assertIsNone(canonical["error_reason"])
        self.assertEqual(canonical_record(decode_record(json.dumps(self.records[2])))["elapsed_seconds"], "4.999998")
        result = assess("event", raw)
        self.assertEqual(result.normalized["SPEED_LEVEL"], {"value": 6, "unit": None})
        self.assertNotIn("ROTATIONAL_SPEED_RPM", result.normalized)
        self.assertEqual(result.raw_values["Timestamp"], "2026-11-01T09:00:00+09:00")
        self.assertEqual(result.mapping_version, "mockprofile-100-c-map-v1")
        self.assertIn("mockprofile-100-c-001", dedup_key(record))

    def test_schema_validation_and_missing_identity(self):
        changed = {**self.record, "Alarm": "YES"}
        with self.assertRaises(ValidationError): decode_record(json.dumps(changed))
        changed = {**self.record, "Readings": {**self.record["Readings"], "SpeedLvl": 11}}
        with self.assertRaises(ValidationError): decode_record(json.dumps(changed))
        changed = {key: value for key, value in self.record.items() if key != "SrcId"}
        self.assertEqual(failure(json.dumps(changed))[0], "MANDATORY_FIELD_MISSING")
        with self.assertRaisesRegex(ValueError, "source schema"):
            decode_record(json.dumps(decode_record(json.dumps(self.record)), default=str))
