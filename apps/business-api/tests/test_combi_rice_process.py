import unittest
from dataclasses import replace
from decimal import Decimal as D

from foodtech_contracts.combi_rice_process import Sample, assess


def records(count=180, humidity=D("96")):
    return [Sample(second, D("100"), humidity, "CLOSED") for second in range(1, count + 1)]


class RiceMonitorTests(unittest.TestCase):
    def test_door_debounce_thresholds_and_missing_evidence(self):
        data = records()
        data[5] = replace(data[5], door="OPEN")
        self.assertEqual(assess(data, 180, "NORMAL", "MEASURED")["door_assessment"]["door_open_seconds"], 0)
        for index in range(10, 20):
            data[index] = replace(data[index], door="OPEN")
        result = assess(data, 180, "NORMAL", "MEASURED")
        self.assertEqual((result["status"], result["door_assessment"]["door_open_seconds"]), ("WARNING", 10))
        self.assertEqual(result["door_assessment"]["door_changes"][0],
                         {"state": "OPEN", "observed_second": 11, "confirmed_second": 12})
        for index in range(20, 25):
            data[index] = replace(data[index], door="OPEN")
        self.assertIn("DOOR_OPEN_FAILURE", assess(data, 180, "NORMAL", "MEASURED")["reason_codes"])
        missing = [replace(item, door=None) for item in records()]
        self.assertEqual(assess(missing, 180, "NORMAL", "MEASURED")["status"], "INSUFFICIENT_DATA")
        missing[0] = replace(missing[0], door="CLOSED")
        with self.assertRaises(ValueError):
            assess(missing, 180, "NORMAL", "MEASURED")
        pending = records()
        pending[-1] = replace(pending[-1], door="OPEN")
        self.assertEqual(assess(pending, 180, "NORMAL", "MEASURED")["status"], "INSUFFICIENT_DATA")
        pending_close = records()
        for index in range(165, 179):
            pending_close[index] = replace(pending_close[index], door="OPEN")
        result = assess(pending_close, 180, "NORMAL", "MEASURED")
        self.assertEqual((result["status"], result["door_assessment"]["door_max_open_seconds"]),
                         ("INSUFFICIENT_DATA", 14))

    def test_low_and_high_duration_with_suspected_sensor_spike(self):
        low = records()
        for i in range(59):
            low[i] = replace(low[i], chamber_celsius=D("94"))
        self.assertNotIn("LOW_TEMPERATURE_PROCESS_FAILURE", assess(low, 180, "NORMAL", "MEASURED")["reason_codes"])
        low[59] = replace(low[59], chamber_celsius=D("94"))
        self.assertIn("LOW_TEMPERATURE_PROCESS_FAILURE", assess(low, 180, "NORMAL", "MEASURED")["reason_codes"])
        high = records()
        for i in range(1, 31):
            high[i] = replace(high[i], chamber_celsius=D("106"))
        result = assess(high, 180, "NORMAL", "MEASURED")
        self.assertIn("HIGH_TEMPERATURE_PROCESS_FAILURE", result["reason_codes"])
        self.assertIn("SENSOR_ANOMALY_SUSPECTED", result["reason_codes"])
        self.assertEqual(result["anomaly_suspected_seconds"], [2])

    def test_humidity_thresholds_and_unsupported(self):
        warning = records(humidity=D("95"))
        result = assess(warning, 180, "NORMAL", "CONTROLLER_ESTIMATE")
        self.assertEqual((result["status"], result["humidity_value_source"]), ("WARNING", "CONTROLLER_ESTIMATE"))
        self.assertIn("LOW_HUMIDITY_WARNING", result["reason_codes"])
        lost = records(humidity=D("89.9"))
        self.assertIn("STEAM_SUPPLY_LOST", assess(lost, 180, "NORMAL", "MEASURED")["reason_codes"])
        unknown = records(humidity=None)
        self.assertEqual(assess(unknown, 180, "NORMAL", "NOT_SUPPORTED")["status"], "INSUFFICIENT_DATA")
        with self.assertRaises(ValueError):
            assess(unknown, 180, "NORMAL", "MEASURED")

    def test_gaps_are_rejected(self):
        data = records()
        data[5] = replace(data[5], second=7)
        with self.assertRaises(ValueError):
            assess(data, 180, "NORMAL", "MEASURED")
