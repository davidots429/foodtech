import unittest
from decimal import Decimal

from foodtech_contracts.jeyuk_safety import JeyukSafetyMonitor


def observe(monitor, second, temperature="75", calibration="OK", addition=False):
    return monitor.observe(
        second, mixture_celsius=Decimal(temperature), calibration_status=calibration,
        safety_sensor_position="LOWEST_MIXTURE_POINT",
        last_ingredient_addition_completed=addition,
    )


class JeyukSafetyTests(unittest.TestCase):
    def test_sixty_seconds_start_after_last_addition(self):
        monitor = JeyukSafetyMonitor()
        self.assertEqual(observe(monitor, 1, "70", addition=True).hold_seconds, 0)
        for second in range(2, 61):
            reading = observe(monitor, second)
        self.assertEqual((reading.status, reading.hold_seconds), ("PENDING", 59))
        reading = observe(monitor, 61)
        self.assertEqual((reading.status, reading.hold_seconds, reading.passed_at_second),
                         ("PASSED", 60, 61))

    def test_cool_sample_resets_hold_and_gap_is_rejected(self):
        monitor = JeyukSafetyMonitor()
        observe(monitor, 1, addition=True)
        for second in range(2, 10):
            observe(monitor, second)
        self.assertEqual(observe(monitor, 10, "74.99").hold_seconds, 0)
        self.assertEqual(observe(monitor, 11).hold_seconds, 1)
        with self.assertRaisesRegex(ValueError, "consecutive"):
            observe(monitor, 13)
        self.assertEqual(monitor.last_second, 11)

    def test_expired_sensor_and_duplicate_addition_do_not_pass(self):
        monitor = JeyukSafetyMonitor()
        observe(monitor, 1, addition=True)
        with self.assertRaisesRegex(ValueError, "already recorded"):
            observe(monitor, 2, addition=True)
        self.assertEqual(monitor.last_second, 1)
        self.assertEqual(observe(monitor, 2, calibration="EXPIRED").status,
                         "ASSESSMENT_INCOMPLETE")
        for second in range(3, 63):
            reading = observe(monitor, second)
        self.assertEqual(reading.status, "ASSESSMENT_INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
