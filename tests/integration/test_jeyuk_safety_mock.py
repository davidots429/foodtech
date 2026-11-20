"""C# synthetic safety fixture proves the post-addition 60-second boundary."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample
from foodtech_contracts.jeyuk_safety import JeyukSafetyMonitor


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-safety-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class JeyukSafetyMockTests(unittest.TestCase):
    def test_csharp_samples_reach_exact_safety_hold_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "safety.jsonl"
            subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(FIXTURE),
                            "--output", str(output)], check=True, capture_output=True)
            raws = output.read_text().splitlines()
        self.assertEqual(len(raws), 61)
        monitor = JeyukSafetyMonitor()
        readings = {}
        for raw in raws:
            sample = decode_jeyuk_mock_sample(raw)
            self.assertEqual(sample.validation_identity, json.loads(FIXTURE.read_text())["validationIdentity"])
            readings[sample.second] = monitor.observe(
                sample.second, mixture_celsius=sample.mixture_celsius,
                calibration_status=sample.calibration_status,
                safety_sensor_position=sample.safety_sensor_position,
                last_ingredient_addition_completed=sample.last_ingredient_addition_completed,
            )
        for checkpoint in json.loads(FIXTURE.read_text())["checkpoints"]:
            reading = readings[checkpoint["second"]]
            self.assertEqual((reading.status, reading.hold_seconds),
                             (checkpoint["autoSafetyStatus"], checkpoint["holdSeconds"]))


if __name__ == "__main__":
    unittest.main()
