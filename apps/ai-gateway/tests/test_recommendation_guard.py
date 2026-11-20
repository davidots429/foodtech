import unittest
from dataclasses import replace
from decimal import Decimal as D

from foodtech_contracts.recommendation_guard import Evidence, TonkatsuProposal, JeyukProposal, tonkatsu, jeyuk


class RecommendationGuardTests(unittest.TestCase):
    def setUp(self):
        self.evidence = Evidence("COMPLETE", "PASSED", True, False)
        self.tonkatsu = TonkatsuProposal("STANDARD", D("170"), 300, 300)
        self.jeyuk = JeyukProposal("SMALL", D("200"), D("30"), 480, D("1.8"), 60, 60,
                                   D("200"), D("30"), 480, D("1.8"), 60, 60, False)

    def test_unknown_or_invalid_evidence_never_approves_values(self):
        for evidence in (replace(self.evidence, completeness="INSUFFICIENT"),
                         replace(self.evidence, safety_status="NOT_MEASURED"),
                         replace(self.evidence, critical_data_valid=False),
                         replace(self.evidence, severe_data_gap=True),
                         replace(self.evidence, severe_data_gap=None)):
            self.assertEqual(tonkatsu(evidence, self.tonkatsu).status, "INDETERMINATE")
            self.assertEqual(jeyuk(evidence, self.jeyuk).status, "INDETERMINATE")
        self.assertEqual(tonkatsu(self.evidence, replace(self.tonkatsu, temperature_c=D("NaN"))).status,
                         "INDETERMINATE")
        self.assertEqual(jeyuk(self.evidence, replace(self.jeyuk, previous_jam_or_overload=None)).status,
                         "INDETERMINATE")

    def test_tonkatsu_limits_and_failed_safety(self):
        self.assertEqual(tonkatsu(self.evidence, self.tonkatsu).status, "WITHIN_ENCODED_LIMITS")
        for proposal in (replace(self.tonkatsu, temperature_c=D("175.1")),
                         replace(self.tonkatsu, duration_seconds=361),
                         replace(self.tonkatsu, duration_seconds=269)):
            self.assertEqual(tonkatsu(self.evidence, proposal).status, "REJECTED")
        failed = replace(self.evidence, safety_status="FAILED")
        self.assertIn("SHORTENING_AFTER_SAFETY_FAILURE",
                      tonkatsu(failed, replace(self.tonkatsu, duration_seconds=299)).reasons)
        self.assertEqual(tonkatsu(failed, replace(self.tonkatsu, previous_duration_seconds=None)).status,
                         "INDETERMINATE")

    def test_jeyuk_target_ranges_and_rated_load_for_all_profiles(self):
        for profile, temperature, rpm, load, seconds in (("SMALL", "200", "30", "3", 480),
                ("MEDIUM", "180", "20", "20", 900), ("LARGE", "160", "10", "100", 1500)):
            proposal = replace(self.jeyuk, profile=profile, temperature_c=D(temperature), rpm=D(rpm),
                               load_kg=D(load), duration_seconds=seconds)
            self.assertEqual(jeyuk(self.evidence, proposal).status, "WITHIN_ENCODED_LIMITS")
            self.assertIn("LOAD_OUTSIDE_RATING", jeyuk(self.evidence, replace(proposal, load_kg=D(load) + D("0.1"))).reasons)
        for proposal in (replace(self.jeyuk, temperature_c=D("205.1")), replace(self.jeyuk, rpm=D("33.1")),
                         replace(self.jeyuk, duration_seconds=541)):
            self.assertEqual(jeyuk(self.evidence, proposal).status, "REJECTED")

    def test_previous_safety_failure_and_jam_restrict_changes(self):
        evidence = replace(self.evidence, safety_status="FAILED")
        proposal = replace(self.jeyuk, temperature_c=D("199"), rpm=D("31"), duration_seconds=479,
                           load_kg=D("2"), vegetable_interval_seconds=59, sauce_interval_seconds=59,
                           previous_jam_or_overload=True)
        result = jeyuk(evidence, proposal)
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(set(result.reasons), {"SHORTENING_AFTER_SAFETY_FAILURE",
            "LOWERING_TEMPERATURE_AFTER_SAFETY_FAILURE", "INCREASED_LOAD_AFTER_JAM", "INCREASED_RPM_AFTER_JAM",
            "SHORTENED_VEGETABLE_INTERVAL_AFTER_JAM", "SHORTENED_SAUCE_INTERVAL_AFTER_JAM"})
        self.assertEqual(jeyuk(evidence, replace(proposal, previous_load_kg=None)).status, "INDETERMINATE")


if __name__ == "__main__":
    unittest.main()
