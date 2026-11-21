import unittest
from dataclasses import replace
from decimal import Decimal as D

from foodtech_contracts.combi_mackerel_process import Sample, assess


def records(count=180):
    return [Sample(second, D("220"), "CLOSED", 3, D("10")) for second in range(1, count + 1)]


class MackerelMonitorTests(unittest.TestCase):
    def test_climate_setting_is_separate_from_humidity_and_required_for_normal(self):
        data = records()
        for value, expected in ((None, "INCOMPLETE"), (D("9"), "DEVIATION_OBSERVED")):
            changed = list(data); changed[10] = replace(changed[10], climate_setpoint_percent=value)
            result = assess(changed, len(changed), "NORMAL")
            self.assertEqual(result["status"], "INSUFFICIENT_DATA")
            self.assertEqual(result["climate_observation"]["status"], expected)
            if value is not None:
                self.assertIn("CLIMATE_SETPOINT_DEVIATION", result["reason_codes"])
        missing = [replace(item, climate_setpoint_percent=None) for item in data]
        self.assertEqual(assess(missing, len(missing), "NORMAL")["climate_observation"]["status"], "NOT_MEASURED")
        invalid = list(data); invalid[0] = replace(invalid[0], climate_setpoint_percent=D("101"))
        with self.assertRaises(ValueError):
            assess(invalid, len(invalid), "NORMAL")

    def test_fan_missing_or_deviation_leaves_process_incomplete(self):
        for fan in (None, 4):
            data = records(); data[10] = replace(data[10], fan_level=fan)
            result = assess(data, len(data), "NORMAL")
            self.assertEqual(result["status"], "INSUFFICIENT_DATA")
            if fan == 4:
                self.assertIn("FAN_DEVIATION", result["reason_codes"])

    def test_recovery_deadline_and_low_temperature(self):
        normal = records()
        for index in range(1, 92):
            normal[index] = replace(normal[index], chamber_celsius=D("210"))
        result = assess(normal, len(normal), "NORMAL")
        self.assertEqual((result["status"], result["recovered_at_second"]), ("NORMAL", 122))
        normal[92] = replace(normal[92], chamber_celsius=D("210"))
        self.assertIn("TEMPERATURE_RECOVERY_FAILURE", assess(normal, len(normal), "NORMAL")["reason_codes"])
        low = records()
        for index in range(40, 70):
            low[index] = replace(low[index], chamber_celsius=D("199"))
        self.assertIn("LOW_TEMPERATURE_PROCESS_FAILURE", assess(low, len(low), "NORMAL")["reason_codes"])

    def test_door_debounce_and_open_thresholds(self):
        data = records()
        data[4] = replace(data[4], door="OPEN")
        self.assertEqual(assess(data, len(data), "NORMAL")["door_open_seconds"], 0)
        for index in range(20, 30):
            data[index] = replace(data[index], door="OPEN")
        warning = assess(data, len(data), "NORMAL")
        self.assertEqual((warning["door_open_seconds"], warning["status"]), (10, "WARNING"))
        self.assertEqual(warning["door_changes"][0],
                         {"state": "OPEN", "observed_second": 21, "confirmed_second": 22})
        for index in range(30, 35):
            data[index] = replace(data[index], door="OPEN")
        failure = assess(data, len(data), "NORMAL")
        self.assertEqual((failure["door_max_open_seconds"], failure["status"]), (15, "FAILED"))

    def test_incomplete_and_invalid_gaps_do_not_pass(self):
        data = records(40)
        data[-1] = replace(data[-1], door="OPEN")
        self.assertEqual(assess(data, len(data), "NORMAL")["status"], "INSUFFICIENT_DATA")
        data[3] = replace(data[3], second=5)
        with self.assertRaises(ValueError):
            assess(data, len(data), "NORMAL")
