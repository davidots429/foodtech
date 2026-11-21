import unittest

from foodtech_contracts.validation_changes import compare_jeyuk_context, recorded_safety_change


class ValidationChangesTests(unittest.TestCase):
    def setUp(self):
        self.context = {"schema_version": "jeyuk-validation-context-v1", "recipe_version": "r1", "equipment_profile": "small", "equipment_device_id": "d1",
                        "source_id": "s1", "ingredient_spec_id": "pork", "ingredient_lot_id": "lot1",
                        "supplier_id": "supplier", "sensor_id": "probe", "sensor_calibration_record_id": "cal1",
                        "process_condition_profile_id": "p1", "process_condition_version": "v1"}

    def test_changed_lot_and_sensor_are_distinguished(self):
        result = compare_jeyuk_context(self.context, {**self.context, "ingredient_lot_id": "lot2",
                                                       "sensor_calibration_record_id": "cal2"})
        self.assertEqual(result.reasons, ("INGREDIENT_CHANGED", "SENSOR_CHANGED"))
        self.assertEqual(result.unknown_fields, ())

    def test_missing_information_is_not_treated_as_a_match(self):
        result = compare_jeyuk_context(self.context, {**self.context, "sensor_id": None})
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.unknown_fields, ("sensor_id",))

    def test_explicit_safety_and_process_events(self):
        self.assertEqual(recorded_safety_change({"domain": "SAFETY_STATUS", "to_status": "FAILED"}), "SAFETY_ANOMALY")
        self.assertEqual(recorded_safety_change({"domain": "SAFETY_STATUS", "from_status": "PASSED",
                                                "to_status": "INCOMPLETE"}), "SAFETY_ANOMALY")
        self.assertEqual(recorded_safety_change({"domain": "JEYUK_FEED", "trigger": "FEED_REVISED"}), "INGREDIENT_RECORD_REVISED")
        self.assertEqual(recorded_safety_change({"domain": "PROCESS_ASSESSMENT", "to_status": "WARNING",
                                                "reason_codes": ["AGITATOR_JAM_WARNING"]}), "PROCESS_ANOMALY")
        self.assertIsNone(recorded_safety_change({"domain": "SAFETY_STATUS", "to_status": "PASSED"}))
        with self.assertRaises(ValueError):
            recorded_safety_change({"domain": "PROCESS_ASSESSMENT", "reason_codes": "FAILED"})


if __name__ == "__main__": unittest.main()
