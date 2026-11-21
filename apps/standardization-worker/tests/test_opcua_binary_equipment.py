import copy
import json
import unittest
from pathlib import Path

from jsonschema.exceptions import ValidationError
from foodtech_contracts import decode_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class EquipmentBinaryTests(unittest.TestCase):
    def test_all_equipment_variants_keep_meaning_and_quality(self):
        for profile in ("213", "223"):
            for vendor in "abc":
                with self.subTest(profile=profile, vendor=vendor):
                    fixture = json.loads((ROOT / f"fixtures/mockprofile-{profile}-{vendor}-001.json").read_text())
                    results = [assess(str(i), json.dumps(row)) for i, row in enumerate(fixture["records"])]
                    self.assertEqual([row.quality_status for row in results], ["VALID", "VALID", "QUARANTINED"])
                    self.assertEqual(results[0].mapping_version, f"mockprofile-{profile}-{vendor}-map-v1")
                    normalized = results[0].normalized
                    self.assertIn("POT_TEMPERATURE" if profile == "213" else "CHAMBER_TEMPERATURE", normalized)
                    if profile == "223":
                        self.assertEqual(normalized["humidity_value_source"], "MEASURED")
                        self.assertIn("RELATIVE_HUMIDITY", normalized)
                    if vendor != "a":
                        self.assertNotIn("ROTATIONAL_SPEED_RPM", normalized)
                    bad = copy.deepcopy(fixture["records"][0])
                    if vendor == "a": bad["extensions"]["equipmentType"] = "OTHER"
                    elif vendor == "b": bad["ext_equipment_type"] = "OTHER"
                    else: bad["Extension"]["EquipmentType"] = "OTHER"
                    with self.assertRaises((ValidationError, ValueError)):
                        decode_record(json.dumps(bad))
                    invalid_identifier = copy.deepcopy(fixture["records"][0])
                    invalid_identifier[{"a": "deviceId", "b": "device_id", "c": "DeviceId"}[vendor]] = []
                    with self.assertRaises(ValueError):
                        decode_record(json.dumps(invalid_identifier))


if __name__ == "__main__": unittest.main()
