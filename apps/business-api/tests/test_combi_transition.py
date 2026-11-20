import unittest
from dataclasses import replace
from decimal import Decimal as D

from foodtech_contracts.combi_transition import TransitionSample, assess_transition


def samples(count, humidity=True):
    return [TransitionSample(i, D("220"), D("10"), True, False, D("15") if humidity else None)
            for i in range(1, count + 1)]


class CombiTransitionTests(unittest.TestCase):
    def test_humidity_supported_and_gap(self):
        self.assertEqual(assess_transition(samples(29), humidity_supported=True, vent_supported=False)["status"],
                         "IN_PROGRESS")
        ready = assess_transition(samples(30), humidity_supported=True, vent_supported=False)
        self.assertEqual((ready["status"], ready["ready_at_second"]), ("DRY_HEAT_READY", 30))
        data = samples(30)
        data[-1] = replace(data[-1], second=31)
        with self.assertRaises(ValueError):
            assess_transition(data, humidity_supported=True, vent_supported=False)

    def test_unsupported_humidity_requires_time_and_ordered_vent(self):
        data = samples(180, False)
        data[2] = replace(data[2], vent_event="OPENED")
        data[5] = replace(data[5], vent_event="CLOSED")
        pending = assess_transition(data[:-1], humidity_supported=False, vent_supported=True)
        self.assertEqual(pending["status"], "IN_PROGRESS")
        result = assess_transition(data, humidity_supported=False, vent_supported=True)
        self.assertEqual((result["status"], result["humidity_verification_status"],
                          result["dryness_verification_basis"]),
                         ("DRY_HEAT_READY", "NOT_SUPPORTED", "TIME_TEMPERATURE_AND_VENT_EVENT"))
        data[2] = replace(data[2], vent_event=None)
        with self.assertRaises(ValueError):
            assess_transition(data, humidity_supported=False, vent_supported=True)

    def test_timeout_and_invalid_supported_humidity(self):
        data = [replace(item, chamber_celsius=D("210")) for item in samples(300)]
        result = assess_transition(data, humidity_supported=True, vent_supported=False)
        self.assertEqual((result["status"], result["mackerel_load_allowed"]), ("MODE_TRANSITION_TIMEOUT", False))
        invalid = samples(30)
        invalid[8] = replace(invalid[8], humidity_percent=None)
        with self.assertRaises(ValueError):
            assess_transition(invalid, humidity_supported=True, vent_supported=False)
