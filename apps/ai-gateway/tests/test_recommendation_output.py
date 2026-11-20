import json
import unittest
from dataclasses import replace
from decimal import Decimal as D

from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import TonkatsuBaseline, JeyukBaseline, validate


class RecommendationOutputTests(unittest.TestCase):
    def setUp(self):
        self.evidence = Evidence("COMPLETE", "PASSED", True, False)
        self.tonkatsu = TonkatsuBaseline("STANDARD", 300)
        self.jeyuk = JeyukBaseline("SMALL", D("200"), D("30"), 480, D("1.8"), 60, 60, False)

    def test_valid_output_is_candidate_for_review_only(self):
        result = validate('{"temperature_c":170,"duration_seconds":300}', self.evidence, self.tonkatsu)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertEqual(result["data"], {"temperature_c": "170", "duration_seconds": 300})
        self.assertFalse(result["fallback_used"])

    def test_malformed_spoofed_or_non_numeric_output_is_empty(self):
        for raw in ('{}', '[]', '{"temperature_c":170,"temperature_c":171,"duration_seconds":300}',
                    '{"temperature_c":NaN,"duration_seconds":300}',
                    '{"temperature_c":"170","duration_seconds":300}',
                    '{"temperature_c":170,"duration_seconds":300.0}',
                    '{"temperature_c":170,"duration_seconds":300,"approved":true}',
                    '{"temperature_c":true,"duration_seconds":300}', 'x' * 16385):
            with self.subTest(raw=raw[:100]):
                result = validate(raw, self.evidence, self.tonkatsu)
                self.assertEqual((result["status"], result["error_code"], result["data"]),
                                 ("FAILED", "STRUCTURED_OUTPUT_INVALID", {}))

    def test_unsafe_or_unknown_evidence_never_returns_candidate(self):
        failed = replace(self.evidence, safety_status="FAILED")
        result = validate('{"temperature_c":170,"duration_seconds":299}', failed, self.tonkatsu)
        self.assertEqual((result["status"], result["data"]), ("REJECTED", {}))
        self.assertIn("SHORTENING_AFTER_SAFETY_FAILURE", result["reasons"])
        result = validate('{"temperature_c":170,"duration_seconds":300}',
                          replace(self.evidence, severe_data_gap=True), self.tonkatsu)
        self.assertEqual((result["status"], result["data"]), ("INDETERMINATE", {}))

    def test_jeyuk_uses_authoritative_baseline_not_model_fields(self):
        value = {"temperature_c": 200, "rpm": 31, "duration_seconds": 480, "load_kg": 2,
                 "vegetable_interval_seconds": 59, "sauce_interval_seconds": 60}
        result = validate(json.dumps(value), self.evidence, replace(self.jeyuk, previous_jam_or_overload=True))
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["data"], {})
        self.assertIn("INCREASED_LOAD_AFTER_JAM", result["reasons"])
        value["previous_jam_or_overload"] = False
        self.assertEqual(validate(json.dumps(value), self.evidence, self.jeyuk)["error_code"],
                         "STRUCTURED_OUTPUT_INVALID")


if __name__ == "__main__":
    unittest.main()
