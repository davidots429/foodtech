import json
import unittest

from foodtech_collection_adapter.combi_stream import CombiStreamValidator


def observation(sequence=1, *, phase="RICE", job="rice-1", second=1):
    return {"eventId": f"meal-1-{sequence:06d}", "mealBatchId": "meal-1", "cookingJobId": job,
            "deviceId": "combi-1", "mockInstanceId": "mockprofile-120-a-001", "eventType": "OBSERVATION",
            "occurredAt": f"2026-01-01T00:00:{sequence-1:02d}Z",
            "receivedAt": f"2026-01-01T00:00:{sequence-1:02d}Z", "sequence": sequence,
            "schemaVersion": "synthetic-combi-v1", "programVersion": "v1", "provenance": "SYNTHETIC",
            "dataQuality": "VALID", "phase": phase, "phaseSecond": second, "chamberCelsius": 100,
            "climateSetpointPercent": 100, "measuredHumidityPercent": 98,
            "humidityValueSource": "MEASURED", "fanLevel": 4, "doorState": "CLOSED"}


class CombiStreamTests(unittest.TestCase):
    def setUp(self):
        self.validator = CombiStreamValidator("meal-1", "combi-1", {
            "RICE": "rice-1", "TRANSITION": "transition-1", "MACKEREL": "fish-1"})

    def test_binding_and_phase_sequence(self):
        self.validator.accept(json.dumps(observation()))
        wrong = observation(2, phase="MACKEREL", job="fish-1")
        with self.assertRaises(ValueError):
            self.validator.accept(json.dumps(wrong))
        transition = observation(2, phase="TRANSITION", job="transition-1")
        transition.update(measuredHumidityPercent=None, humidityValueSource="NOT_SUPPORTED")
        self.assertEqual(self.validator.accept(json.dumps(transition))["phase"], "TRANSITION")
        fish = observation(3, phase="MACKEREL", job="fish-1")
        self.assertEqual(self.validator.accept(json.dumps(fish))["cookingJobId"], "fish-1")

    def test_rejects_foreign_identity_gaps_and_unsupported_values(self):
        for changes in ({"cookingJobId": "other"}, {"sequence": 2}, {"mealBatchId": "foreign"},
                        {"humidityValueSource": "NOT_SUPPORTED"}, {"chamberCelsius": True}):
            with self.assertRaises(ValueError):
                self.validator.accept(json.dumps({**observation(), **changes}))
        self.assertEqual(self.validator.accept(json.dumps(observation()))["sequence"], 1)
