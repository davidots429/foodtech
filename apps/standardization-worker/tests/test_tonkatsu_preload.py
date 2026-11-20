import unittest
from decimal import Decimal

from foodtech_contracts.tonkatsu_preload_observation import observe


class PreplacementObservationTests(unittest.TestCase):
    def setUp(self):
        self.binding = {"source_id": "source", "device_id": "device", "batch_id": "batch"}
        self.job = {"source_binding": self.binding, "actual_placed_at": "2026-11-14T00:00:10Z"}

    def sample(self, second, seq, temperature="170", quality="VALID"):
        return {"dedup_status": "UNIQUE", "quality_status": quality, "normalized": {**self.binding,
                "device_status": "RUNNING", "event_time": f"2026-11-14T00:00:{second:02}Z",
                "sequence_number": Decimal(seq), "CHAMBER_TEMPERATURE": {"value": temperature, "unit": "°C"}}}

    def test_latest_eligible_before_placement_is_reported_with_age_and_quality(self):
        before = self.sample(5, 2, "168.5", "WARNING")
        after = self.sample(10, 3, "171")
        result = observe([after, self.sample(0, 1), before], self.job)
        self.assertEqual((result["status"], result["sample_celsius"], result["quality_status"]),
                         ("OBSERVED", "168.5", "WARNING"))
        self.assertEqual((result["age_before_placement_us"], result["sequence_number"]), (5_000_000, 2))

    def test_ambiguous_and_ineligible_points_do_not_claim_temperature(self):
        result = observe([self.sample(5, 1, "160"), self.sample(5, 2, "180")], self.job)
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertNotIn("sample_celsius", result)
        invalid = self.sample(5, 2)
        invalid["normalized"]["batch_id"] = "other"
        self.assertEqual(observe([invalid, self.sample(5, 3, quality="QUARANTINED")], self.job)["status"], "NOT_CAPTURED")
        self.assertEqual(observe([self.sample(5, 1)], {**self.job, "actual_placed_at": None})["status"], "NOT_CAPTURED")
