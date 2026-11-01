import unittest
from datetime import datetime, timedelta, timezone

from foodtech_standardization_worker.process_rules import advance


def sample(second, temperature):
    at = (datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat().replace("+00:00", "Z")
    return {"event_time": at, "CHAMBER_TEMPERATURE": {"value": str(temperature), "unit": "°C"}}


class ProcessRuleTests(unittest.TestCase):
    def step(self, state, second, temperature, *, findings=None, quality="VALID"):
        return advance(state, sample(second, temperature), ingestion_event_id=f"event-{second}",
                       quality_status=quality, finding_codes=findings or set())

    def test_high_failure_requires_twenty_continuous_seconds(self):
        state = None
        for second in (0, 5, 10, 15):
            state = self.step(state, second, 181)
            self.assertEqual(state["status"], "WARNING")
        state = self.step(state, 20, 181)
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["temperature_status"], "HIGH_PROCESS_FAILURE")

    def test_gap_breaks_continuous_failure_and_quarantine_is_ignored(self):
        state = self.step(None, 0, 181)
        state = self.step(state, 5, 181)
        self.assertIsNone(self.step(state, 10, -300, quality="QUARANTINED"))
        state = self.step(state, 20, 181, findings={"DATA_GAP"})
        self.assertEqual(state["status"], "WARNING")
        self.assertTrue(state["data_gap"])
        for second in (25, 30, 35):
            state = self.step(state, second, 181)
        self.assertEqual(state["status"], "WARNING")
        state = self.step(state, 40, 181)
        self.assertEqual(state["status"], "FAILED")

    def test_low_continuous_and_recovery_boundary(self):
        state = None
        for second in (0, 5, 10, 15):
            state = self.step(state, second, 159)
        self.assertEqual(state["status"], "WARNING")
        self.assertEqual(self.step(state, 20, 159)["status"], "FAILED")

        state = None
        for second in range(0, 95, 5):
            state = self.step(state, second, 162)
        self.assertEqual(state["status"], "WARNING")
        self.assertEqual(self.step(state, 95, 165)["status"], "FAILED")

    def test_low_cumulative_time_across_short_bouts(self):
        state = None
        for cycle in range(9):
            base = cycle * 20
            for second in (base, base + 5, base + 10):
                state = self.step(state, second, 159)
            if cycle < 8:
                self.assertNotEqual(state["status"], "FAILED")
                state = self.step(state, base + 15, 170)
        self.assertEqual(state["low_accumulated_seconds"], 90)
        self.assertEqual(state["status"], "FAILED")

    def test_old_event_does_not_rewind_state(self):
        state = self.step(None, 5, 170)
        self.assertIsNone(self.step(state, 0, 181))
        self.assertIsNone(self.step(state, 5, 181))
        self.assertIsNone(self.step(state, 10, 181, findings={"LATE_AFTER_GAP"}))


if __name__ == "__main__":
    unittest.main()
