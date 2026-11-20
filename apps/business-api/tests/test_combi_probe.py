import unittest
from datetime import datetime, timezone
from decimal import Decimal

from foodtech_contracts.combi_probe import summarize


class ProbeSummaryTests(unittest.TestCase):
    def test_gap_and_missing_value_never_complete_the_interval(self):
        product = {"rack_id": "rack", "shelf": 2, "pan_id": "pan", "mass_kg": "0.13", "thickness_cm": "1.5"}
        probe = {"probe_id": "probe", "product_id": "fish", "calibration_status": "OK",
                 "readings": [{"second": i, "celsius": Decimal("75")} for i in range(60)]}
        probe["readings"][10]["celsius"] = None
        probe["readings"][30]["second"] = 29
        result = summarize(probe, product, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "ASSESSMENT_INCOMPLETE")
        self.assertIsNone(result["full_interval_min_celsius"])
        self.assertEqual(result["longest_75c_hold_seconds"], 29)
        self.assertEqual(result["measurement_window_end_at"], "2026-01-01T00:01:00Z")
