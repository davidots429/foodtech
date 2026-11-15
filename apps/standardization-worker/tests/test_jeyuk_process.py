import json
import unittest
from decimal import Decimal
from pathlib import Path

from foodtech_contracts.jeyuk_process import (PROFILES, ProcessMonitor, cooking_time_status,
                                              load_status, rpm_band, surface_band)


FIXTURE = Path(__file__).resolve().parents[3] / "fixtures/jeyuk-small-process-v1.json"


def sample(monitor: ProcessMonitor, second: int, *, surface: str = "200", rpm: str = "30",
           target: str = "30", motor_load: str = "50", event: str | None = None):
    return monitor.observe(second, surface_celsius=Decimal(surface), actual_rpm=Decimal(rpm),
                           target_rpm=Decimal(target), motor_load_percent=Decimal(motor_load), event=event)


class JeyukProcessTests(unittest.TestCase):
    def test_approved_boundaries_for_all_profiles(self):
        for name, profile in PROFILES.items():
            with self.subTest(name=name):
                self.assertEqual(surface_band(name, profile.surface_low_failure - Decimal("0.01")),
                                 "LOW_DEVIATION")
                self.assertEqual(surface_band(name, profile.surface_low_failure), "LOW_WARNING")
                self.assertEqual(surface_band(name, profile.surface_low_normal), "NORMAL")
                self.assertEqual(surface_band(name, profile.surface_high_normal), "NORMAL")
                self.assertEqual(surface_band(name, profile.surface_high_failure), "HIGH_WARNING")
                self.assertEqual(surface_band(name, profile.surface_high_failure + Decimal("0.01")),
                                 "HIGH_DEVIATION")
                self.assertEqual(rpm_band(name, profile.rpm_low_failure - Decimal("0.01"),
                                          profile.rpm_low_normal), "LOW_DEVIATION")
                self.assertEqual(rpm_band(name, profile.rpm_low_failure, profile.rpm_low_normal),
                                 "LOW_WARNING")
                self.assertEqual(rpm_band(name, profile.rpm_low_normal, profile.rpm_low_normal), "NORMAL")
                self.assertEqual(rpm_band(name, profile.rpm_high_normal, profile.rpm_low_normal), "NORMAL")
                self.assertEqual(rpm_band(name, profile.rpm_high_failure, profile.rpm_low_normal),
                                 "HIGH_WARNING")
                self.assertEqual(rpm_band(name, profile.rpm_high_failure + Decimal("0.01"),
                                          profile.rpm_low_normal), "HIGH_DEVIATION")
                self.assertEqual(load_status(name, profile.recommended_load_kg), "NORMAL")
                self.assertEqual(load_status(name, profile.recommended_load_kg + Decimal("0.01")),
                                 "OVER_RECOMMENDED_LOAD")
                self.assertEqual(load_status(name, profile.rated_load_kg), "OVER_RECOMMENDED_LOAD")
                self.assertEqual(load_status(name, profile.rated_load_kg + Decimal("0.01")),
                                 "RATED_LOAD_EXCEEDED")
                early, short, normal, delay, maximum = profile.time_bands
                self.assertEqual(cooking_time_status(name, early - 1), "EARLY_REMOVAL")
                self.assertEqual(cooking_time_status(name, early), "SHORT_WARNING")
                self.assertEqual(cooking_time_status(name, short), "NORMAL")
                self.assertEqual(cooking_time_status(name, normal), "NORMAL")
                self.assertEqual(cooking_time_status(name, normal + 1), "DELAY_WARNING")
                self.assertEqual(cooking_time_status(name, delay + 1), "EXTENDED_COOKING")
                self.assertEqual(cooking_time_status(name, maximum + 1), "MAX_TIME_EXCEEDED")

    def test_fixture_jam_uses_one_second_raw_samples(self):
        fixture = json.loads(FIXTURE.read_text())
        monitor = ProcessMonitor(fixture["profile"], Decimal(fixture["actualLoadKg"]))
        actual = {}
        second = 0
        for segment in fixture["samples"]:
            for _ in range(segment["seconds"]):
                second += 1
                actual[second] = monitor.observe(second,
                    surface_celsius=Decimal(segment["surfaceCelsius"]),
                    actual_rpm=Decimal(segment["actualRpm"]),
                    target_rpm=Decimal(segment["targetRpm"]),
                    motor_load_percent=Decimal(segment["motorLoadPercent"]))
        for checkpoint in fixture["checkpoints"]:
            with self.subTest(second=checkpoint["second"]):
                reading = actual[checkpoint["second"]]
                self.assertEqual(reading.status, checkpoint["status"])
                self.assertIn(checkpoint["reasonCode"], reading.reason_codes)
        self.assertNotIn("LOW_RPM_WARNING", actual[5].reason_codes)
        self.assertNotIn("HIGH_MOTOR_LOAD_WARNING", actual[5].reason_codes)
        self.assertEqual(sample(monitor, 31).status, "FAILED")

    def test_surface_failure_consecutive_and_cumulative(self):
        monitor = ProcessMonitor("SMALL", Decimal("1.8"))
        for second in range(1, 45):
            reading = sample(monitor, second, surface="184")
        self.assertEqual(reading.status, "WARNING")
        self.assertIn("LOW_SURFACE_TEMPERATURE", sample(monitor, 45, surface="184").reason_codes)
        cumulative = ProcessMonitor("SMALL", Decimal("1.8"))
        second = 0
        for _ in range(6):
            for _ in range(30):
                second += 1
                reading = sample(cumulative, second, surface="184")
            if second < 185:
                second += 1
                sample(cumulative, second)
        self.assertEqual(reading.status, "FAILED")
        self.assertEqual(cumulative.low_surface_cumulative, 180)
        high = ProcessMonitor("SMALL", Decimal("1.8"))
        for second in range(1, 30):
            reading = sample(high, second, surface="216")
        self.assertEqual(reading.status, "WARNING")
        self.assertIn("HIGH_SURFACE_TEMPERATURE", sample(high, 30, surface="216").reason_codes)

    def test_rpm_duration_recovery_and_stop(self):
        low = ProcessMonitor("SMALL", Decimal("1.8"))
        sample(low, 1)
        for second in range(2, 61):
            reading = sample(low, second, rpm="20")
        self.assertEqual(reading.status, "WARNING")
        self.assertIn("LOW_RPM_DURATION", sample(low, 61, rpm="20").reason_codes)
        recovery = ProcessMonitor("SMALL", Decimal("1.8"))
        for second in range(1, 46):
            reading = sample(recovery, second, rpm="25")
        self.assertEqual(reading.status, "WARNING")
        self.assertIn("RPM_RECOVERY_TIMEOUT", sample(recovery, 46, rpm="25").reason_codes)
        stopped = ProcessMonitor("SMALL", Decimal("1.8"))
        reading = sample(stopped, 1, rpm="0", target="0")
        self.assertEqual((reading.status, reading.rpm_band), ("NORMAL", "AGITATION_NOT_REQUIRED"))
        self.assertEqual(sample(stopped, 2, rpm="30").status, "NORMAL")

    def test_hard_events_and_missing_seconds(self):
        monitor = ProcessMonitor("SMALL", Decimal("1.8"))
        sample(monitor, 1)
        with self.assertRaises(ValueError):
            sample(monitor, 3)
        self.assertEqual(monitor.last_second, 1)
        self.assertIn("AGITATOR_FAILURE",
                      sample(monitor, 2, event="EMERGENCY_STOPPED").reason_codes)
        with self.assertRaises(ValueError):
            load_status("SMALL", Decimal("NaN"))
        with self.assertRaises(ValueError):
            cooking_time_status("SMALL", -1)

    def test_cumulative_low_rpm_and_high_rpm_failure(self):
        low = ProcessMonitor("SMALL", Decimal("1.8"))
        second = 1
        sample(low, second)
        for _ in range(4):
            for _ in range(30):
                second += 1
                reading = sample(low, second, rpm="20")
            if second < 124:
                second += 1
                sample(low, second)
        self.assertEqual(low.low_rpm_cumulative, 120)
        self.assertIn("LOW_RPM_DURATION", reading.reason_codes)
        high = ProcessMonitor("SMALL", Decimal("1.8"))
        for second in range(1, 30):
            reading = sample(high, second, rpm="38")
        self.assertEqual(reading.status, "WARNING")
        self.assertIn("HIGH_RPM_DURATION", sample(high, 30, rpm="38").reason_codes)
        overloaded = ProcessMonitor("SMALL", Decimal("3.01"))
        self.assertIn("RATED_LOAD_EXCEEDED", sample(overloaded, 1).reason_codes)


if __name__ == "__main__":
    unittest.main()
