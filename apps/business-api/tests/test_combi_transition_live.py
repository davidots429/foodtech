import unittest
from decimal import Decimal
from foodtech_contracts.combi_transition_live import observe
from foodtech_contracts.combi_transition import TransitionSample, assess_transition


def row(second, humidity='10', source='MEASURED', vent=False, event=None, temp='220', alarm=False,
        door='CLOSED', schema='synthetic-combi-v2'):
    result = {'phase': 'TRANSITION', 'phaseSecond': second, 'chamberCelsius': Decimal(temp),
              'climateSetpointPercent': Decimal('10'), 'doorState': door, 'humidityValueSource': source,
              'measuredHumidityPercent': Decimal(humidity) if humidity is not None else None,
              'schemaVersion': schema}
    if schema == 'synthetic-combi-v2': result.update(criticalAlarm=alarm, ventSupported=vent, ventEvent=event)
    return result


def run(rows):
    state = None
    for sample in rows: state = observe(state, sample)
    return state


def offline(rows, humidity=True, vent=False):
    return assess_transition([TransitionSample(s['phaseSecond'], s['chamberCelsius'], s['climateSetpointPercent'],
        s['doorState'] == 'CLOSED', s['criticalAlarm'], s['measuredHumidityPercent'], s['ventEvent'])
        for s in rows], humidity_supported=humidity, vent_supported=vent)


class TransitionLiveTests(unittest.TestCase):
    def test_measured_ready_at_30_and_reverts_if_conditions_change(self):
        rows = [row(i) for i in range(1, 31)]
        self.assertIsNone(run(rows[:-1])['ready_observed_at_second'])
        state = run(rows)
        self.assertEqual(state['ready_observed_at_second'], 30)
        self.assertEqual(offline(rows)['ready_at_second'], 30)
        state = observe(state, row(31, door='OPEN'))
        self.assertFalse(state['ready_conditions_current'])
        self.assertEqual(state['ready_observed_at_second'], 30)

    def test_unsupported_requires_180_and_vent_sequence(self):
        rows = [row(i, None, 'NOT_SUPPORTED', True, 'OPENED' if i == 20 else 'CLOSED' if i == 21 else None)
                for i in range(1, 181)]
        self.assertIsNone(run(rows[:-1])['ready_observed_at_second'])
        state = run(rows)
        self.assertEqual((state['ready_observed_at_second'], state['vent_status']), (180, 'CLOSED'))
        self.assertEqual(offline(rows, humidity=False, vent=True)['ready_at_second'], 180)
        missing = [row(i, None, 'NOT_SUPPORTED', True) for i in range(1, 181)]
        self.assertIsNone(run(missing)['ready_observed_at_second'])

    def test_timeout_and_missing_evidence(self):
        rows = [row(i, temp='200') for i in range(1, 301)]
        self.assertFalse(run(rows[:-1])['timeout'])
        self.assertTrue(run(rows)['timeout'])
        self.assertEqual(offline(rows)['status'], 'MODE_TRANSITION_TIMEOUT')
        estimated = run([row(i, source='CONTROLLER_ESTIMATE') for i in range(1, 301)])
        self.assertFalse(estimated['complete_from_start'])
        self.assertFalse(estimated['timeout'])
        legacy = run([row(i, schema='synthetic-combi-v1') for i in range(1, 31)])
        self.assertFalse(legacy['complete_from_start'])
        self.assertEqual(legacy['vent_status'], 'UNKNOWN')
        self.assertIsNone(legacy['ready_observed_at_second'])
        bad_vent = run([row(1, None, 'NOT_SUPPORTED', True, 'CLOSED')])
        self.assertFalse(bad_vent['complete_from_start'])
        self.assertFalse(observe(None, row(2))['complete_from_start'])

    def test_capability_change_remains_incomplete(self):
        state = run([row(1), row(2, None, 'NOT_SUPPORTED')])
        self.assertFalse(state['complete_from_start'])
        changed_vent = run([row(1), row(2, vent=True)])
        self.assertFalse(changed_vent['complete_from_start'])
        self.assertEqual(changed_vent['vent_status'], 'UNKNOWN')
        with self.assertRaises(ValueError): observe(state, row(4))
