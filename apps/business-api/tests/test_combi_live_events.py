import unittest
from foodtech_contracts.combi_live_events import events_for


def sample(sequence=1, phase='RICE', door='CLOSED', alarm=False, vent=None):
    return {'sequence': sequence, 'phase': phase, 'phaseSecond': sequence,
            'occurredAt': '2026-01-01T00:00:00Z', 'doorState': door,
            'criticalAlarm': alarm, 'ventEvent': vent}


class LiveEventTests(unittest.TestCase):
    def test_first_phase_and_new_warning_failure_have_original_sequence(self):
        result = events_for(sample(60), previous_phase=None, previous_door=None, previous_alarm=None,
            before={'warning_codes': ['LOW_TEMPERATURE_WARNING'], 'failure_codes': []},
            after={'warning_codes': ['LOW_TEMPERATURE_WARNING'], 'failure_codes': ['LOW_TEMPERATURE_PROCESS_FAILURE']})
        self.assertEqual([event['code'] for event in result], ['PHASE_STARTED', 'LOW_TEMPERATURE_PROCESS_FAILURE'])
        self.assertEqual(result[1]['severity'], 'FAILURE')
        self.assertTrue(all(event['sequence'] == 60 and event['provenance'] == 'SYNTHETIC' for event in result))

    def test_door_alarm_vent_and_ready_transitions(self):
        result = events_for(sample(3, 'TRANSITION', 'OPEN', True, 'OPENED'), previous_phase='RICE',
            previous_door='CLOSED', previous_alarm=False, before={'ready_observed_at_second': None},
            after={'ready_observed_at_second': 3})
        self.assertEqual([item['code'] for item in result],
            ['PHASE_STARTED', 'DOOR_OPENED', 'CRITICAL_ALARM_ACTIVATED', 'VENT_OPENED', 'TRANSITION_READY_OBSERVED'])
        later = events_for(sample(4, 'TRANSITION', 'CLOSED', False, 'CLOSED'), previous_phase='TRANSITION',
            previous_door='OPEN', previous_alarm=True, before={'ready_observed_at_second': 3},
            after={'ready_observed_at_second': 3, 'timeout': True})
        self.assertEqual([item['code'] for item in later], ['DOOR_CLOSED', 'CRITICAL_ALARM_CLEARED', 'VENT_CLOSED', 'MODE_TRANSITION_TIMEOUT'])

    def test_existing_failure_does_not_repeat(self):
        same = {'failure_codes': ['LOW_TEMPERATURE_PROCESS_FAILURE'], 'warning_codes': []}
        result = events_for(sample(61), previous_phase='RICE', previous_door='CLOSED',
                            previous_alarm=False, before=same, after=same)
        self.assertEqual(result, [])
