import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from foodtech_contracts.combi_cooking import cooking_durations, cooking_time_status, oven_load, rice_water


class CombiCookingContractTests(unittest.TestCase):
    def test_program_duration_excludes_unloading_delay(self):
        start = datetime(2026, 11, 20, tzinfo=timezone.utc)
        completed = start + timedelta(seconds=720)
        self.assertEqual(cooking_durations(start, completed, completed + timedelta(seconds=30)), (720, 750))
        with self.assertRaises(ValueError):
            cooking_durations(start, completed, completed - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            cooking_durations(start.replace(tzinfo=None), completed, completed)

    def test_program_duration_boundaries_are_menu_specific(self):
        for menu, values in {
            "RICE": (1679, 1680, 1739, 1740, 1860, 1861, 1920, 1921),
            "MACKEREL": (659, 660, 689, 690, 750, 751, 780, 781),
        }.items():
            self.assertEqual([cooking_time_status(menu, value) for value in values],
                             ["EARLY_TERMINATION", "SHORT_WARNING", "SHORT_WARNING", "NORMAL",
                              "NORMAL", "DELAY_WARNING", "DELAY_WARNING", "MAX_TIME_EXCEEDED"])
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                cooking_time_status("RICE", value)

    def test_rice_water_calculates_absorption_added_target_and_range(self):
        water = rice_water(D("2"), D("2.4"), D("2.0"))
        self.assertEqual((water.absorbed_and_residual_water_kg, water.target_added_water_kg,
                          water.actual_effective_water_kg, water.effective_water_ratio, water.status),
                         (D("0.4"), D("2.0"), D("2.4"), D("1.2"), "NORMAL"))
        self.assertEqual(rice_water(D("2"), D("2.4"), D("1.9")).status, "NORMAL")
        self.assertEqual(rice_water(D("2"), D("2.4"), D("2.1")).status, "NORMAL")
        self.assertEqual(rice_water(D("2"), D("2.4"), D("2.11")).status, "WATER_RATIO_OUT_OF_RANGE")
        self.assertIsNone(rice_water(D("2"), D("4.5"), D("0")).target_added_water_kg)
        with self.assertRaises(ValueError):
            rice_water(D("2"), D("1.9"), D("1"))

    def test_approved_maximum_batches_and_multiple_limits(self):
        medium = oven_load("MEDIUM_FIXED_RACK_10", 1, 10, D("44"), D("10"), D("0"))
        self.assertEqual((medium.payload_kg, medium.gross_kg, medium.rated_capacity_kg, medium.violations),
                         (D("54"), D("54"), D("60"), ()))
        large = oven_load("LARGE_ROLL_IN_RACK_20", 2, 20, D("88"), D("20"), D("30"))
        self.assertEqual((large.payload_kg, large.gross_kg, large.load_ratio, large.violations),
                         (D("108"), D("138"), D("0.8625"), ()))
        overloaded = oven_load("LARGE_ROLL_IN_RACK_20", 3, 21, D("111"), D("20"), D("30"))
        self.assertEqual(overloaded.violations,
                         ("RACK_COUNT_EXCEEDED", "PAN_COUNT_EXCEEDED", "RATED_LOAD_EXCEEDED"))
        with self.assertRaises(ValueError):
            oven_load("MEDIUM_FIXED_RACK_10", 1, 1, D("1"), D("1"), D("1"))


if __name__ == "__main__":
    unittest.main()
