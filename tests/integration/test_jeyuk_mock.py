"""C# Jeyuk simulator output must drive the approved platform process rules."""

import json
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from foodtech_contracts.jeyuk_process import ProcessMonitor
from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-process-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class JeyukMockTests(unittest.TestCase):
    def test_one_second_output_reaches_approved_warning_and_failure(self):
        for size in ("small", "medium", "large"):
            with self.subTest(size=size):
                self.check_failure_fixture(ROOT / f"fixtures/jeyuk-{size}-process-v2.json", size)

    def check_failure_fixture(self, fixture, size):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "jeyuk.jsonl"
            completed = subprocess.run(
                ["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(fixture),
                 "--output", str(output)], check=True, capture_output=True, text=True,
            )
            metadata = json.loads(completed.stdout)
            records = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(metadata["count"], 30)
        self.assertEqual([item["second"] for item in records], list(range(1, 31)))
        self.assertEqual({item["sampleIntervalSeconds"] for item in records}, {1})
        self.assertEqual({item["profileId"] for item in records}, {"mockprofile-110"})
        self.assertEqual({item["instanceId"] for item in records}, {"mockprofile-110-a-001"})
        self.assertEqual({item["provenance"] for item in records}, {"SYNTHETIC"})
        self.assertEqual({item["profile"] for item in records}, {size.upper()})
        self.assertEqual({item["heatingSetpointCelsius"] for item in records},
                         { {"small": 200, "medium": 180, "large": 160}[size] })
        self.assertEqual(decode_jeyuk_mock_sample(json.dumps(records[0])).heating_setpoint_celsius,
                         Decimal(str(records[0]["heatingSetpointCelsius"])))
        legacy = dict(records[0]); del legacy["heatingSetpointCelsius"]
        self.assertIsNone(decode_jeyuk_mock_sample(json.dumps(legacy)).heating_setpoint_celsius)
        legacy["heatingSetpointCelsius"] = None
        with self.assertRaises(ValueError): decode_jeyuk_mock_sample(json.dumps(legacy))
        monitor = ProcessMonitor(records[0]["profile"], Decimal(str(records[0]["actualLoadKg"])))
        readings = {}
        for record in records:
            readings[record["second"]] = monitor.observe(
                record["second"], surface_celsius=Decimal(str(record["surfaceCelsius"])),
                actual_rpm=Decimal(str(record["actualRpm"])),
                target_rpm=Decimal(str(record["targetRpm"])),
                motor_load_percent=Decimal(str(record["motorLoadPercent"])),
            )
        for checkpoint in json.loads(fixture.read_text())["checkpoints"]:
            reading = readings[checkpoint["second"]]
            self.assertEqual(reading.status, checkpoint["status"])
            self.assertIn(checkpoint["reasonCode"], reading.reason_codes)

    def test_full_v2_fixtures_have_explicit_profile_setpoints(self):
        for size, target in (("small", 200), ("medium", 180), ("large", 160)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "samples.jsonl"
                subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture",
                                str(ROOT / f"fixtures/jeyuk-{size}-full-v2.json"), "--output", str(output)],
                               check=True, capture_output=True)
                records = [decode_jeyuk_mock_sample(line) for line in output.read_text().splitlines()]
                self.assertTrue(records)
                self.assertEqual({item.heating_setpoint_celsius for item in records}, {Decimal(target)})
                self.assertEqual({item.fixture_id for item in records}, {f"jeyuk-{size}-full-v2"})

    def test_invalid_setpoint_does_not_create_output(self):
        fixture = json.loads(FIXTURE.read_text())
        for invalid in ("-1", None, "not-a-number"):
            fixture["samples"][0]["heatingSetpointCelsius"] = invalid
            with self.subTest(value=invalid), tempfile.TemporaryDirectory() as directory:
                source, output = Path(directory) / "invalid.json", Path(directory) / "output.jsonl"
                source.write_text(json.dumps(fixture))
                completed = subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(source),
                                            "--output", str(output)], capture_output=True, text=True)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(output.exists())

    def test_invalid_duration_does_not_create_output(self):
        fixture = json.loads(FIXTURE.read_text())
        fixture["samples"][1]["seconds"] = 0
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.json"
            output = Path(directory) / "invalid.jsonl"
            source.write_text(json.dumps(fixture))
            completed = subprocess.run(
                ["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(source),
                 "--output", str(output)], capture_output=True, text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
