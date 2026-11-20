import copy
import json
import unittest
from pathlib import Path

from jsonschema import ValidationError
from foodtech_contracts import decode_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class CombiUaJsonTests(unittest.TestCase):
    def test_humidity_source_and_fan_are_not_conflated(self):
        for vendor in "abc":
            fixture = json.loads((ROOT / f"fixtures/mockprofile-220-{vendor}-001.json").read_text())
            row = fixture["records"][0]
            names = {"a": ("humiditySource", "humidityPercent"),
                     "b": ("ext_humidity_source", "ext_humidity_percent"),
                     "c": ("HumiditySource", "HumidityPct")}[vendor]
            for source, value in (("MEASURED", 98), ("CONTROLLER_ESTIMATE", 90), ("NOT_SUPPORTED", None)):
                record = copy.deepcopy(row)
                target = record["extensions"] if vendor == "a" else record if vendor == "b" else record["Extension"]
                target[names[0]], target[names[1]] = source, value
                result = assess("test", json.dumps(record))
                with self.subTest(vendor=vendor, source=source):
                    self.assertEqual(result.mapping_version, f"mockprofile-220-{vendor}-map-v1")
                    self.assertEqual(result.normalized["CHAMBER_TEMPERATURE"]["unit"], "°C")
                    self.assertEqual(result.normalized["CLIMATE_SETTING_PERCENT"], {"value": "100", "unit": "%"})
                    self.assertEqual(result.normalized["FAN_SPEED_LEVEL"]["value"], 4)
                    self.assertEqual(result.normalized["humidity_value_source"], source)
                    self.assertEqual("RELATIVE_HUMIDITY" in result.normalized, source == "MEASURED")
                    self.assertEqual("HUMIDITY_ESTIMATE_PERCENT" in result.normalized, source == "CONTROLLER_ESTIMATE")
            target[names[0]], target[names[1]] = "NOT_SUPPORTED", 98
            with self.assertRaises(ValidationError):
                decode_record(json.dumps(record))

    def test_contradictory_vendor_c_fan_levels_are_rejected(self):
        fixture = json.loads((ROOT / "fixtures/mockprofile-220-c-001.json").read_text())
        row = fixture["records"][0]
        row["Readings"]["SpeedLvl"] = 6
        with self.assertRaisesRegex(ValueError, "conflicting synthetic fan levels"):
            decode_record(json.dumps(row))


if __name__ == "__main__":
    unittest.main()
