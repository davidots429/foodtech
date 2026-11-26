"""Recorded values only: no AI calls, history certification or AWS requests."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from uuid import uuid4

from foodtech_contracts.jeyuk_observed_inputs import observe_jeyuk_batch


class ObservedJeyukInputsTests(unittest.TestCase):
    def setUp(self):
        self.placed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        iso = lambda offset: (self.placed + timedelta(seconds=offset)).isoformat()
        self.job = {'job_id': str(uuid4()), 'owner_subject': 'private-owner-marker', 'notes': 'private-note-marker',
            'menu': 'JEYUK', 'provenance': 'SYNTHETIC', 'jeyuk_size_profile': 'SMALL',
            'equipment_profile': 'jeyuk-small', 'actual_cooking_seconds': Decimal(6), 'actual_load_kg': '1.8',
            'actual_placed_at': iso(0), 'actual_lifted_at': iso(6), 'safety_status': 'PASSED',
            'source_binding': {'source_id': 'synthetic-rest', 'device_id': 'mockprofile-110-a-001', 'batch_id': 'jeyuk-batch'},
            'jeyuk_addition_group_id': 'group', 'jeyuk_additions_simultaneous': True,
            'jeyuk_feeds': {name: {'ingredient': name, 'provenance': 'SYNTHETIC', 'started_at': iso(start),
                'completed_at': iso(end), 'addition_group_id': 'group', 'mass_kg': mass,
                'initial_temperature_c': '4', 'temperature_observed_at': iso(-3)} for name, start, end, mass in
                [('MEAT', -2, 0, '1.0'), ('VEGETABLE', 1, 1.5, '0.5'), ('SAUCE', 2, 2.5, '0.3')]}}
        self.raws = []
        for second in range(1, 7):
            sample = {'fixtureId': 'jeyuk-batch', 'provenance': 'SYNTHETIC', 'profileId': 'mockprofile-110',
                'instanceId': 'mockprofile-110-a-001', 'profile': 'SMALL', 'actualLoadKg': 1.8, 'second': second,
                'sampleIntervalSeconds': 1, 'surfaceCelsius': 190, 'heatingSetpointCelsius': 200,
                'actualRpm': 10, 'targetRpm': 30, 'motorLoadPercent': 40, 'mixtureCelsius': 75,
                'calibrationStatus': 'OK', 'safetySensorPosition': 'LOWEST_MIXTURE_POINT',
                'lastIngredientAdditionCompleted': second == 3, 'observedAt': iso(second)}
            self.raws.append({'pk': 'JEYUK#jeyuk-batch', 'sk': f'RAW#{second:06d}',
                              'provenance': 'SYNTHETIC', 'raw': json.dumps(sample)})

    def change(self, row, **changes):
        value = json.loads(row['raw'])
        value.update(changes)
        row['raw'] = json.dumps(value)

    def test_baseline_uses_settings_and_actual_duration_never_measured_surface_or_defaults(self):
        value = observe_jeyuk_batch(self.job, self.raws)
        baseline = value.as_baseline()
        self.assertEqual(baseline.previous_temperature_c, Decimal('200'))
        self.assertEqual(baseline.previous_rpm, Decimal('30'))
        self.assertEqual(baseline.previous_duration_seconds, 6)
        self.assertEqual(baseline.previous_load_kg, Decimal('1.8'))
        self.assertEqual((baseline.previous_vegetable_interval_seconds, baseline.previous_sauce_interval_seconds), (1, 2))
        self.assertFalse(baseline.previous_jam_or_overload)
        self.assertEqual(value.recorded_safety_status, 'PASSED')
        self.assertEqual(value.sampled_mixture_safety.status, 'PENDING')
        self.assertEqual(value.sampled_mixture_safety.hold_seconds, 3)
        self.assertTrue(value.sampled_final_feed_aligned)
        self.assertTrue(value.feed_mass_matches_load)
        self.assertEqual(value.initial_temperatures_c, (Decimal(4),) * 3)
        self.assertNotIn('completeness', value.model_context())
        self.assertNotIn('critical_data_valid', value.model_context())

    def test_missing_or_changing_settings_are_not_filled_from_profile_targets_or_averages(self):
        row = json.loads(self.raws[0]['raw'])
        del row['heatingSetpointCelsius']
        self.raws[0]['raw'] = json.dumps(row)
        self.change(self.raws[1], targetRpm=20)
        value = observe_jeyuk_batch(self.job, self.raws)
        self.assertIsNone(value.heating_setpoint_c)
        self.assertIsNone(value.target_rpm)
        self.assertIn('HEATING_SETPOINT_NOT_STABLE_OR_MISSING', value.unresolved)
        self.assertIn('TARGET_RPM_NOT_STABLE_OR_MISSING', value.unresolved)

    def test_fractional_feed_offsets_are_not_silently_rounded(self):
        self.job['jeyuk_feeds']['VEGETABLE']['started_at'] = (self.placed + timedelta(seconds=1, microseconds=1)).isoformat()
        value = observe_jeyuk_batch(self.job, self.raws)
        self.assertIsNone(value.vegetable_offset_seconds)
        self.assertEqual(value.sauce_offset_seconds, 2)

    def test_observed_jam_and_overload_protect_baseline_even_if_final_sample_is_normal(self):
        for row in self.raws[:5]:
            self.change(row, motorLoadPercent=95)
        self.assertTrue(observe_jeyuk_batch(self.job, self.raws).sampled_jam_or_overload)
        for row in self.raws:
            self.change(row, actualRpm=30, motorLoadPercent=40)
        self.change(self.raws[0], motorLoadPercent=101)
        self.assertTrue(observe_jeyuk_batch(self.job, self.raws).sampled_jam_or_overload)

    def test_gap_mixed_batch_load_profile_or_timeline_is_not_a_usable_observation(self):
        cases = []
        rows = copy.deepcopy(self.raws)
        cases.append(rows[:-1])
        rows = copy.deepcopy(self.raws)
        self.change(rows[2], observedAt=(self.placed + timedelta(seconds=4)).isoformat())
        cases.append(rows)
        rows = copy.deepcopy(self.raws)
        self.change(rows[2], actualLoadKg=2)
        cases.append(rows)
        rows = copy.deepcopy(self.raws)
        rows[0]['pk'] = 'JEYUK#foreign'
        cases.append(rows)
        for rows in cases:
            with self.assertRaisesRegex(ValueError, '^synthetic batch observation unavailable$'):
                observe_jeyuk_batch(self.job, rows)

    def test_missing_safety_is_unknown_and_not_copied_from_job_passed_label(self):
        for row in self.raws:
            sample = json.loads(row['raw'])
            for key in ('mixtureCelsius', 'calibrationStatus', 'safetySensorPosition', 'lastIngredientAdditionCompleted'):
                del sample[key]
            row['raw'] = json.dumps(sample)
        value = observe_jeyuk_batch(self.job, self.raws)
        self.assertIsNone(value.sampled_mixture_safety)
        self.assertIn('MIXTURE_SAFETY_SAMPLES_MISSING', value.unresolved)

    def test_prompt_context_excludes_identity_notes_and_raw_source_payloads(self):
        value = observe_jeyuk_batch(self.job, self.raws)
        context = json.dumps(value.model_context())
        for text in [self.job['job_id'], 'private-owner-marker', 'private-note-marker', 'jeyuk-batch', 'fixtureId']:
            self.assertNotIn(text, context)
        self.assertNotIn(self.job['job_id'], repr(value))

    def test_initial_observation_time_mass_mismatch_and_calibration_remain_distinct_facts(self):
        self.job['jeyuk_feeds']['MEAT']['temperature_observed_at'] = (self.placed - timedelta(seconds=1)).isoformat()
        self.job['jeyuk_feeds']['VEGETABLE']['mass_kg'] = '0.7'
        self.change(self.raws[0], calibrationStatus='EXPIRED')
        value = observe_jeyuk_batch(self.job, self.raws)
        self.assertIsNone(value.initial_temperatures_c[0])
        self.assertFalse(value.feed_mass_matches_load)
        self.assertFalse(value.sampled_calibration_all_ok)
        self.assertIn('INITIAL_TEMPERATURE_RECORD_UNVERIFIED_MEAT', value.unresolved)
        self.assertIn('FEED_MASS_MISMATCH', value.unresolved)

    def test_a_prior_pass_event_does_not_hide_a_later_zero_hold(self):
        self.job['actual_cooking_seconds'] = 64
        self.job['actual_lifted_at'] = (self.placed + timedelta(seconds=64)).isoformat()
        rows = []
        for second in range(1, 65):
            row = copy.deepcopy(self.raws[0])
            row['sk'] = f'RAW#{second:06d}'
            self.change(row, second=second, observedAt=(self.placed + timedelta(seconds=second)).isoformat(),
                        lastIngredientAdditionCompleted=second == 3, mixtureCelsius=74 if second == 64 else 75)
            rows.append(row)
        value = observe_jeyuk_batch(self.job, rows)
        self.assertEqual(value.sampled_mixture_safety.status, 'PASSED')
        self.assertEqual(value.sampled_mixture_safety.hold_seconds, 0)
        self.assertEqual(value.sampled_mixture_safety.passed_at_second, 63)
        self.assertEqual(value.model_context()['sampled_mixture_safety']['hold_seconds'], 0)
