"""C# synthetic safety fixture proves the post-addition 60-second boundary."""

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample
from foodtech_contracts.jeyuk_safety import JeyukSafetyMonitor
from foodtech_standardization_worker.jeyuk_bridge import JeyukProcessStream


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-safety-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class JeyukSafetyMockTests(unittest.TestCase):
    def test_delayed_addition_preserves_samples_and_moves_safety_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ROOT / "fixtures/jeyuk-small-full-v1.json"
            bound = Path(directory) / "bound.json"
            original_output = Path(directory) / "original.jsonl"
            output = Path(directory) / "delayed.jsonl"
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            command = ["dotnet", str(DLL), "bind-jeyuk", "--fixture", str(fixture), "--output", str(bound),
                       "--fixture-id", "jeyuk-delayed-test", "--start-at", start.isoformat(),
                       "--last-addition-second", "120"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertNotIn("checkpoints", json.loads(bound.read_text()))
            for source, target in ((fixture, original_output), (bound, output)):
                subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(source), "--output", str(target)],
                               check=True, capture_output=True)
            before = [decode_jeyuk_mock_sample(line) for line in original_output.read_text().splitlines()]
            after = [decode_jeyuk_mock_sample(line) for line in output.read_text().splitlines()]
            self.assertEqual(len(after), 480)
            self.assertEqual([sample.second for sample in after if sample.last_ingredient_addition_completed], [120])
            self.assertEqual([(x.surface_celsius, x.actual_rpm, x.mixture_celsius) for x in before],
                             [(x.surface_celsius, x.actual_rpm, x.mixture_celsius) for x in after])
            self.assertTrue(all(sample.observed_at == start + timedelta(seconds=sample.second) for sample in after))
            monitor = JeyukSafetyMonitor()
            readings = {s.second: monitor.observe(s.second, mixture_celsius=s.mixture_celsius,
                calibration_status=s.calibration_status, safety_sensor_position=s.safety_sensor_position,
                last_ingredient_addition_completed=s.last_ingredient_addition_completed) for s in after}
            self.assertEqual((readings[119].status, readings[179].hold_seconds, readings[180].status),
                             ("PENDING", 59, "PASSED"))
            for invalid in ("0", "481", "1.5"):
                bad = Path(directory) / f"invalid-{invalid}.json"
                args = command.copy(); args[args.index(str(bound))] = str(bad); args[-1] = invalid
                self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
                self.assertFalse(bad.exists())

    def test_bound_fixture_generates_a_timestamp_for_every_second(self):
        with tempfile.TemporaryDirectory() as directory:
            bound = Path(directory) / "bound.json"
            output = Path(directory) / "samples.jsonl"
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            command = ["dotnet", str(DLL), "bind-jeyuk", "--fixture", str(FIXTURE), "--output", str(bound),
                       "--fixture-id", "jeyuk-timeline-test", "--start-at", start.isoformat()]
            subprocess.run(command, check=True, capture_output=True)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(bound), "--output", str(output)],
                           check=True, capture_output=True)
            samples = [decode_jeyuk_mock_sample(line) for line in output.read_text().splitlines()]
            self.assertEqual(len(samples), 61)
            self.assertTrue(all(sample.observed_at == start + timedelta(seconds=i)
                                for i, sample in enumerate(samples, 1)))
            invalid = Path(directory) / "invalid.json"
            invalid_command = ["dotnet", str(DLL), "bind-jeyuk", "--fixture", str(FIXTURE), "--output", str(invalid),
                               "--fixture-id", "invalid", "--start-at", "2026-01-01T00:00:00"]
            self.assertNotEqual(subprocess.run(invalid_command, capture_output=True).returncode, 0)
            self.assertFalse(invalid.exists())

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
