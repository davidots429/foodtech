import unittest
from copy import deepcopy

from foodtech_business_api.recommendation_completeness import preliminary


def sample_probe(item_id, temperature="75"):
    return {"item_id": item_id, "calibration_status": "OK",
            "samples": [{"at_second": second, "celsius": temperature} for second in range(61)]}


class CompletenessTests(unittest.TestCase):
    def setUp(self):
        self.job = {"menu": "TONKATSU", "provenance": "SYNTHETIC", "safety_status": "PASSED",
                    "planned_portions": 1, "tonkatsu_batch": {"product_count": 2},
                    "equipment_profile": "synthetic-fryer-approved-v2", "thickness_profile": "STANDARD",
                    "actual_cooking_seconds": 300, "core_check": [sample_probe("a"), sample_probe("b")]}
        self.initial = {"ingredient_temperature_c": "4", "temperature_observed_at": "2026-11-21T00:00:00Z"}

    def evaluate(self, job=None, initial=None, coverage=None):
        return preliminary(job or self.job, initial_context=self.initial if initial is None else initial,
                           coverage=coverage)

    def test_fully_recorded_critical_values_do_not_claim_full_completeness(self):
        result = self.evaluate(coverage={"internal_gap_detected": False})
        self.assertEqual(result["status"], "NOT_ASSESSED")
        self.assertEqual(result["reasons"], ["SOURCE_COMPLETENESS_NOT_ASSESSED"])

    def test_invalid_incomplete_or_inconsistent_core_series_is_insufficient(self):
        variants = []
        for mutate in (lambda j: j["core_check"].pop(),
                       lambda j: j["core_check"][0]["samples"].pop(),
                       lambda j: j["core_check"][0].update(calibration_status="EXPIRED"),
                       lambda j: j["core_check"][0]["samples"][5].update(celsius="NaN"),
                       lambda j: j["core_check"][0]["samples"][5].update(celsius="74"),
                       lambda j: j["core_check"][1].update(item_id="a")):
            job = deepcopy(self.job); mutate(job); variants.append(job)
        for job in variants:
            with self.subTest(job=job):
                result = self.evaluate(job)
                self.assertEqual(result["status"], "INSUFFICIENT")
                self.assertIn("CORE_SERIES_MISSING_INVALID_OR_INCONSISTENT", result["reasons"])

    def test_failed_safety_with_valid_low_value_is_not_misclassified_as_invalid(self):
        job = deepcopy(self.job); job["safety_status"] = "FAILED"
        job["core_check"][0]["samples"][2]["celsius"] = "74"
        result = self.evaluate(job)
        self.assertEqual(result["status"], "NOT_ASSESSED")
        self.assertNotIn("CORE_SERIES_MISSING_INVALID_OR_INCONSISTENT", result["reasons"])

    def test_detected_gap_does_not_invent_severity(self):
        result = self.evaluate(coverage={"internal_gap_detected": True})
        self.assertEqual(result["status"], "NOT_ASSESSED")
        self.assertIn("GAP_SEVERITY_NOT_ASSESSED", result["reasons"])

    def test_missing_critical_values_and_other_menu(self):
        job = deepcopy(self.job); job.update(safety_status="NOT_MEASURED", actual_cooking_seconds=None)
        result = self.evaluate(job, initial={})
        self.assertEqual(result["status"], "INSUFFICIENT")
        self.assertIn("INITIAL_TEMPERATURE_MISSING", result["reasons"])
        self.assertIn("COOKING_DURATION_MISSING", result["reasons"])
        self.assertEqual(self.evaluate({"menu": "JEYUK", "provenance": "SYNTHETIC"})["status"], "NOT_ASSESSED")
