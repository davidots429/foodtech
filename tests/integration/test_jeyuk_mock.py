"""C# Jeyuk simulator output must drive the approved platform process rules."""

import json
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from foodtech_contracts.jeyuk_process import ProcessMonitor


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-process-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class JeyukMockTests(unittest.TestCase):
    def test_one_second_output_reaches_approved_warning_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "jeyuk.jsonl"
            completed = subprocess.run(
                ["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(FIXTURE),
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
        monitor = ProcessMonitor(records[0]["profile"], Decimal(str(records[0]["actualLoadKg"])))
        readings = {}
        for record in records:
            readings[record["second"]] = monitor.observe(
                record["second"], surface_celsius=Decimal(str(record["surfaceCelsius"])),
                actual_rpm=Decimal(str(record["actualRpm"])),
                target_rpm=Decimal(str(record["targetRpm"])),
                motor_load_percent=Decimal(str(record["motorLoadPercent"])),
            )
        for checkpoint in json.loads(FIXTURE.read_text())["checkpoints"]:
            reading = readings[checkpoint["second"]]
            self.assertEqual(reading.status, checkpoint["status"])
            self.assertIn(checkpoint["reasonCode"], reading.reason_codes)

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
