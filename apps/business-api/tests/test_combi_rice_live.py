import unittest
from decimal import Decimal
from foodtech_contracts.combi_rice_live import observe
from foodtech_contracts.combi_rice_process import Sample, assess


def sample(second, temp='100', humidity='98', source='MEASURED'):
    return {'phase': 'RICE', 'phaseSecond': second, 'chamberCelsius': Decimal(temp),
            'humidityValueSource': source, 'measuredHumidityPercent': Decimal(humidity) if humidity is not None else None}


def run(readings):
    state = None
    for row in readings:
        state = observe(state, row)
    return state


class RiceLiveTests(unittest.TestCase):
    def test_low_and_high_boundaries_match_offline_temperature_rules(self):
        for temp, count, reason in [('94', 60, 'LOW_TEMPERATURE_PROCESS_FAILURE'),
                                    ('106', 30, 'HIGH_TEMPERATURE_PROCESS_FAILURE')]:
            rows = [sample(i, temp) for i in range(1, count + 1)]
            before, current = run(rows[:-1]), run(rows)
            self.assertNotIn(reason, before['failure_codes'])
            self.assertIn(reason, current['failure_codes'])
            offline = assess([Sample(i, Decimal(temp), Decimal('98'), 'CLOSED', 4) for i in range(1, count + 1)],
                             count, 'NORMAL', 'MEASURED')
            self.assertIn(reason, offline['reason_codes'])

    def test_cumulative_temperature_failure_without_continuous_limit(self):
        for temp, groups, length, code in [('94', 4, 45, 'LOW_TEMPERATURE_PROCESS_FAILURE'),
                                           ('106', 4, 23, 'HIGH_TEMPERATURE_PROCESS_FAILURE')]:
            values = []
            for _ in range(groups):
                values.extend([temp] * length + ['100'])
            state = run([sample(i, value) for i, value in enumerate(values, 1)])
            self.assertIn(code, state['failure_codes'])
            self.assertEqual(state['low_run' if temp == '94' else 'high_run'], 0)

    def test_humidity_boundaries_and_sensor_anomaly(self):
        for humidity, count, key, code in [('95', 60, 'warning_codes', 'LOW_HUMIDITY_WARNING'),
                                           ('89', 120, 'failure_codes', 'STEAM_SUPPLY_LOST')]:
            rows = [sample(i, humidity=humidity) for i in range(1, count + 1)]
            self.assertNotIn(code, run(rows[:-1])[key])
            self.assertIn(code, run(rows)[key])
        state = run([sample(1, '100'), sample(2, '106')])
        self.assertIn('SENSOR_ANOMALY_SUSPECTED', state['warning_codes'])
        self.assertEqual(state['high_total'], 1)

    def test_incomplete_source_and_missing_history(self):
        state = observe(None, sample(3, humidity=None, source='NOT_SUPPORTED'))
        self.assertFalse(state['complete_from_start'])
        state = observe(state, sample(4, humidity=None, source='NOT_SUPPORTED'))
        self.assertFalse(state['complete_from_start'])
        with self.assertRaises(ValueError): observe(state, sample(6))
        state = run([sample(1), sample(2, humidity=None, source='NOT_SUPPORTED')])
        self.assertFalse(state['complete_from_start'])
        self.assertEqual(state['humidity_failure_run'], 0)
