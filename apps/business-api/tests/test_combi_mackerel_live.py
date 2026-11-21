import unittest
from decimal import Decimal
from foodtech_contracts.combi_mackerel_live import observe
from foodtech_contracts.combi_mackerel_process import Sample, assess


def record(second, temp='220', door='CLOSED'):
    return {'phase': 'MACKEREL', 'phaseSecond': second, 'chamberCelsius': Decimal(temp), 'doorState': door}


def run(rows):
    state = None
    for row in rows:
        state = observe(state, row)
    return state


def offline(rows):
    samples = [Sample(row['phaseSecond'], row['chamberCelsius'], row['doorState'], 3, Decimal('10')) for row in rows]
    return assess(samples, len(rows), 'NORMAL')


class MackerelLiveTests(unittest.TestCase):
    def test_recovery_and_post_recovery_low_boundary(self):
        rows = [record(i) for i in range(1, 32)]
        self.assertIsNone(run(rows[:-1])['recovered_at_second'])
        self.assertEqual(run(rows)['recovered_at_second'], 31)
        rows.extend(record(i, '199') for i in range(32, 62))
        self.assertNotIn('LOW_TEMPERATURE_PROCESS_FAILURE', run(rows[:-1])['failure_codes'])
        state = run(rows)
        self.assertIn('LOW_TEMPERATURE_PROCESS_FAILURE', state['failure_codes'])
        self.assertIn('LOW_TEMPERATURE_PROCESS_FAILURE', offline(rows)['reason_codes'])

    def test_recovery_deadline_inclusive(self):
        rows = [record(i, '200' if i <= 92 else '220') for i in range(1, 123)]
        self.assertEqual(run(rows)['recovered_at_second'], 122)
        self.assertNotIn('TEMPERATURE_RECOVERY_FAILURE', run(rows)['failure_codes'])
        late = [record(i, '200' if i <= 93 else '220') for i in range(1, 123)]
        self.assertNotIn('TEMPERATURE_RECOVERY_FAILURE', run(late[:-1])['failure_codes'])
        self.assertIn('TEMPERATURE_RECOVERY_FAILURE', run(late)['failure_codes'])
        self.assertIn('TEMPERATURE_RECOVERY_FAILURE', offline(late)['reason_codes'])

    def test_debounced_open_and_unconfirmed_close(self):
        rows = [record(i, door='OPEN' if 3 <= i <= 16 else 'CLOSED') for i in range(1, 18)]
        state = run(rows)
        self.assertEqual((state['door_open_seconds'], state['candidate_door']), (14, 'CLOSED'))
        self.assertNotIn('DOOR_OPEN_FAILURE', state['failure_codes'])
        rows.append(record(18))
        state = run(rows)
        self.assertEqual(state['door_open_seconds'], 14)
        self.assertIn('DOOR_OPEN_WARNING', state['warning_codes'])
        self.assertEqual(offline(rows)['door_open_seconds'], 14)
        rows[16] = record(17, door='OPEN')
        state = run(rows[:17])
        self.assertEqual(state['door_open_seconds'], 15)
        self.assertIn('DOOR_OPEN_FAILURE', state['failure_codes'])

    def test_accumulated_open_failure_across_short_openings(self):
        doors = ['CLOSED', 'CLOSED'] + (['OPEN'] * 10 + ['CLOSED'] * 2) * 3
        rows = [record(i, door=door) for i, door in enumerate(doors, 1)]
        state = run(rows)
        self.assertEqual((state['door_open_seconds'], state['door_max_open_seconds']), (30, 10))
        self.assertIn('DOOR_OPEN_FAILURE', state['failure_codes'])
        self.assertIn('DOOR_OPEN_FAILURE', offline(rows)['reason_codes'])

    def test_short_bounce_and_missing_history(self):
        state = run([record(1), record(2), record(3, door='OPEN'), record(4)])
        self.assertEqual(state['door_open_seconds'], 0)
        self.assertIsNone(state['candidate_door'])
        state = observe(None, record(10))
        self.assertFalse(state['complete_from_start'])
        self.assertIsNone(state['recovered_at_second'])
        state = run([record(1, door='OPEN'), record(2), record(3)])
        self.assertFalse(state['complete_from_start'])
        with self.assertRaises(ValueError): observe(state, record(5))
