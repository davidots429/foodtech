import json
import unittest
from pathlib import Path

from foodtech_collection_adapter.ingestion_quarantine import failure
from foodtech_contracts import canonical_record, decode_record, dedup_key
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class VendorBTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((ROOT / "fixtures/mockprofile-100-b-001.json").read_text())["records"][0]

    def test_source_to_standard_preserves_distinct_output_type(self):
        raw = json.dumps(self.record)
        self.assertIsNone(failure(raw))
        self.assertIsNone(failure(json.dumps({**self.record, "deviceId": "unknown-extra-field"})))
        self.assertEqual(decode_record(json.dumps({**self.record, "deviceId": "mockprofile-100-b-001"}))["deviceId"],
                         "mockprofile-100-b-001")
        record = decode_record(raw)
        canonical = canonical_record(record)
        self.assertEqual(canonical["temperature_celsius"], "180")
        self.assertEqual(canonical["output_percent"], "60")
        self.assertNotIn("speed_rpm", canonical)
        self.assertEqual(canonical["elapsed_seconds"], "0")
        self.assertEqual(canonical["device_status"], "running")
        result = assess("event", raw)
        self.assertEqual(result.normalized["OUTPUT_PERCENT"], {"value": "60", "unit": "%"})
        self.assertNotIn("ROTATIONAL_SPEED_RPM", result.normalized)
        self.assertEqual(result.raw_values["temperature_f"], "356")
        self.assertEqual(result.mapping_version, "mockprofile-100-b-map-v1")
        self.assertIn("mockprofile-100-b-001", dedup_key(record))
        with self.assertRaisesRegex(ValueError, "source schema"):
            decode_record(json.dumps(record, default=str))

    def test_invalid_values_and_missing_identity_are_not_guessed(self):
        changed = {**self.record, "temperature_f": 356.01}
        with self.assertRaisesRegex(ValueError, "not exactly reversible"):
            decode_record(json.dumps(changed))
        changed = {**self.record, "device_status": 9}
        with self.assertRaises(Exception):
            decode_record(json.dumps(changed))
        changed = {key: value for key, value in self.record.items() if key != "source_id"}
        self.assertEqual(failure(json.dumps(changed))[0], "MANDATORY_FIELD_MISSING")
