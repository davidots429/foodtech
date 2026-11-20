import json
import unittest
from decimal import Decimal

from foodtech_business_api.jeyuk_source_evidence import audit_source_records
from foodtech_contracts.jeyuk_process import ProcessMonitor, RULE_VERSION as PROCESS_RULE_VERSION
from foodtech_contracts.jeyuk_safety import JeyukSafetyMonitor, RULE_VERSION as SAFETY_RULE_VERSION


def fixture(count=61, *, warning_at=None, expired_at=None):
    batch = "jeyuk-evidence-a"
    pk = f"JEYUK#{batch}"
    assessments = []
    raws = []
    identity = {"ingredientSpecId": "pork-v1", "ingredientLotId": "lot-v1", "supplierId": "supplier-v1",
                "sensorId": "sensor-v1", "sensorCalibrationRecordId": "cal-v1",
                "processConditionProfileId": "profile-v1", "processConditionVersion": "v1"}
    process = ProcessMonitor("SMALL", Decimal("1.8"))
    safety = JeyukSafetyMonitor()
    for second in range(1, count + 1):
        raw_sk = f"RAW#{second:06d}"
        raw = {"fixtureId": batch, "provenance": "SYNTHETIC", "profileId": "mockprofile-110",
               "instanceId": "mockprofile-110-a-001", "profile": "SMALL", "actualLoadKg": 1.8,
               "second": second, "sampleIntervalSeconds": 1,
               "surfaceCelsius": 190 if second == warning_at else 200,
               "actualRpm": 30, "targetRpm": 30, "motorLoadPercent": 50,
               "mixtureCelsius": 75, "calibrationStatus": "EXPIRED" if second == expired_at else "OK",
               "safetySensorPosition": "LOWEST_MIXTURE_POINT",
               "validationIdentity": identity,
               "lastIngredientAdditionCompleted": second == 1}
        raws.append({"pk": pk, "sk": raw_sk, "raw": json.dumps(raw)})
        process_reading = process.observe(second, surface_celsius=Decimal(raw["surfaceCelsius"]),
                                          actual_rpm=Decimal(30), target_rpm=Decimal(30),
                                          motor_load_percent=Decimal(50))
        safety_reading = safety.observe(second, mixture_celsius=Decimal(75),
                                       calibration_status=raw["calibrationStatus"],
                                       safety_sensor_position="LOWEST_MIXTURE_POINT",
                                       last_ingredient_addition_completed=second == 1)
        assessments.append({"pk": pk, "sk": f"ASSESSMENT#{second:06d}", "raw_sk": raw_sk,
                            "fixture_id": batch, "second": second, "profile_id": "mockprofile-110",
                            "profile": "SMALL", "actual_load_kg": "1.8", "provenance": "SYNTHETIC",
                            "process_status": process_reading.status,
                            "reason_codes": list(process_reading.reason_codes),
                            "surface_band": process_reading.surface_band,
                            "rpm_band": process_reading.rpm_band,
                            "load_status": process_reading.load_status,
                            "auto_safety_status": safety_reading.status,
                            "mixture_hold_seconds": safety_reading.hold_seconds,
                            "last_addition_second": safety_reading.last_addition_second,
                            "safety_passed_at_second": safety_reading.passed_at_second,
                            "safety_rule_version": SAFETY_RULE_VERSION})
    job = {"source_binding": {"batch_id": batch},
           "validation_context": {"actual_load_kg": "1.80", "ingredient_spec_id": "pork-v1",
                                  "ingredient_lot_id": "lot-v1", "supplier_id": "supplier-v1",
                                  "sensor_id": "sensor-v1", "sensor_calibration_record_id": "cal-v1",
                                  "process_condition_profile_id": "profile-v1", "process_condition_version": "v1"},
           "process_assessment": {"second": count, "source_assessment_sk": f"ASSESSMENT#{count:06d}",
                                  "status": "NORMAL", "rule_version": PROCESS_RULE_VERSION},
           "auto_safety_status": safety_reading.status,
           "mixture_hold_seconds": safety_reading.hold_seconds,
           "safety_passed_at_second": safety_reading.passed_at_second,
           "actual_cooking_seconds": count,
           "process_time_status": "NORMAL", "process_assessment_status": "NORMAL"}
    return job, assessments, raws


class JeyukSourceEvidenceTests(unittest.TestCase):
    def test_complete_source_range_matches_without_activating_validation(self):
        job, assessments, raws = fixture()
        self.assertTrue(all(audit_source_records(job, assessments, raws).values()))

    def test_gap_and_wrong_batch_fail_closed(self):
        job, assessments, raws = fixture()
        self.assertFalse(audit_source_records(job, assessments[:-1], raws)["source_records_contiguous"])
        raws[0]["raw"] = raws[0]["raw"].replace("jeyuk-evidence-a", "jeyuk-other-a")
        checked = audit_source_records(job, assessments, raws)
        self.assertTrue(checked["source_records_contiguous"])
        self.assertFalse(checked["source_records_match_binding"])

    def test_derived_result_tampering_is_rejected(self):
        job, assessments, raws = fixture()
        assessments[5]["surface_band"] = "HIGH_WARNING"
        checked = audit_source_records(job, assessments, raws)
        self.assertFalse(checked["source_derived_results_match_raw"])

    def test_identity_mismatch_or_legacy_sample_does_not_match(self):
        job, assessments, raws = fixture()
        raw = json.loads(raws[3]["raw"])
        raw["validationIdentity"]["ingredientLotId"] = "another-lot"
        raws[3]["raw"] = json.dumps(raw)
        self.assertFalse(audit_source_records(job, assessments, raws)["source_validation_identity_matches"])
        del raw["validationIdentity"]
        raws[3]["raw"] = json.dumps(raw)
        self.assertFalse(audit_source_records(job, assessments, raws)["source_validation_identity_matches"])

    def test_warning_expired_calibration_and_short_span_remain_unverified(self):
        job, assessments, raws = fixture(warning_at=6, expired_at=7)
        job["actual_cooking_seconds"] = 62
        checked = audit_source_records(job, assessments, raws)
        self.assertTrue(checked["source_records_match_binding"])
        self.assertTrue(checked["source_derived_results_match_raw"])
        self.assertFalse(checked["source_process_conditions_normal"])
        self.assertFalse(checked["source_safety_samples_valid"])
        self.assertFalse(checked["source_process_span_covers_cooking"])


if __name__ == "__main__":
    unittest.main()
