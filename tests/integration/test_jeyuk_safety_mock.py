"""C# synthetic safety fixture proves the post-addition 60-second boundary."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample
from foodtech_contracts.jeyuk_safety import JeyukSafetyMonitor
from foodtech_standardization_worker.jeyuk_bridge import JeyukProcessStream


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-safety-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class JeyukSafetyMockTests(unittest.TestCase):
    def test_csharp_samples_reach_exact_safety_hold_boundary(self):
        for size in ("small", "medium", "large"):
            with self.subTest(size=size):
                self.check_fixture(ROOT / f"fixtures/jeyuk-{size}-safety-v1.json", size, 61)

    def test_full_duration_profiles_keep_process_and_safety_observations(self):
        for size, seconds in (("small", 480), ("medium", 900), ("large", 1500)):
            with self.subTest(size=size):
                self.check_fixture(ROOT / f"fixtures/jeyuk-{size}-full-v1.json", size, seconds)

    def check_fixture(self, fixture, size, seconds):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "safety.jsonl"
            subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(fixture),
                            "--output", str(output)], check=True, capture_output=True)
            raws = output.read_text().splitlines()
        self.assertEqual(len(raws), seconds)
        monitor = JeyukSafetyMonitor()
        process = JeyukProcessStream()
        readings = {}
        fixture_data = json.loads(fixture.read_text())
        for raw in raws:
            sample = decode_jeyuk_mock_sample(raw)
            self.assertEqual(sample.profile, size.upper())
            self.assertNotEqual(process.accept(raw).status, "FAILED")
            self.assertEqual(sample.validation_identity, fixture_data["validationIdentity"])
            readings[sample.second] = monitor.observe(
                sample.second, mixture_celsius=sample.mixture_celsius,
                calibration_status=sample.calibration_status,
                safety_sensor_position=sample.safety_sensor_position,
                last_ingredient_addition_completed=sample.last_ingredient_addition_completed,
            )
        for checkpoint in fixture_data["checkpoints"]:
            reading = readings[checkpoint["second"]]
            self.assertEqual((reading.status, reading.hold_seconds),
                             (checkpoint["autoSafetyStatus"], checkpoint["holdSeconds"]))


if __name__ == "__main__":
    unittest.main()
