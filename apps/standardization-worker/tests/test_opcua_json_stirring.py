import json
import unittest
from pathlib import Path

from jsonschema import ValidationError
from foodtech_contracts import decode_record
from foodtech_contracts.profile100 import canonical_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class StirringUaJsonTests(unittest.TestCase):
    def test_each_vendor_preserves_units_and_pot_temperature(self):
        for vendor in "abc":
            with self.subTest(vendor=vendor):
                fixture = json.loads((ROOT / f"fixtures/mockprofile-210-{vendor}-001.json").read_text())
                results = [assess(str(i), json.dumps(record)) for i, record in enumerate(fixture["records"])]
                self.assertEqual([row.quality_status for row in results], ["VALID", "VALID", "QUARANTINED"])
                self.assertTrue(all(row.mapping_version == f"mockprofile-210-{vendor}-map-v1" for row in results))
                self.assertEqual(results[0].normalized["POT_TEMPERATURE"], {"value": "180", "unit": "°C"})
                self.assertNotIn("CHAMBER_TEMPERATURE", results[0].normalized)
                speed = {"a": "ROTATIONAL_SPEED_RPM", "b": "OUTPUT_PERCENT", "c": "SPEED_LEVEL"}[vendor]
                self.assertIn(speed, results[0].normalized)
                for other in {"ROTATIONAL_SPEED_RPM", "OUTPUT_PERCENT", "SPEED_LEVEL"} - {speed}:
                    self.assertNotIn(other, results[0].normalized)

    def test_equipment_identity_is_required_and_cannot_be_spoofed(self):
        for vendor in "abc":
            fixture = json.loads((ROOT / f"fixtures/mockprofile-210-{vendor}-001.json").read_text())
            raw = fixture["records"][0]
            if vendor == "a": raw["extensions"]["equipmentType"] = "OVEN"
            elif vendor == "b": raw["ext_equipment_type"] = "OVEN"
            else: raw["Extension"]["EquipmentType"] = "OVEN"
            with self.subTest(vendor=vendor), self.assertRaises(ValidationError):
                decode_record(json.dumps(raw))
            canonical = decode_record(json.dumps(fixture["records"][1]))
            canonical["extensions"] = {}
            with self.subTest(canonical=vendor), self.assertRaises(ValidationError):
                canonical_record(canonical)


if __name__ == "__main__":
    unittest.main()
