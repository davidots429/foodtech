from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
import unittest

from foodtech_contracts.combi_quality_observation import observe_quality, SCORE_ITEMS
from foodtech_ai_gateway.combi_recommendation_inputs import build_combi_inputs
from test_combi_observed_inputs import fixture, NOW, iso
from foodtech_contracts.combi_observed_inputs import observe_combi_batch


class CombiQualityObservationTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.raw = fixture()
        self.meta = self.rows['META']
        self.step = self.rows['JOB#RICE#' + self.meta['rice_cooking_job_id']]
        self.pointer = self.step['quality_current']
        self.event = self.rows[self.pointer['event_sk']]
        self.a = self.event['assessment']

    def read(self):
        return dict(observe_quality(self.rows, self.step, self.meta, 'RICE', now=NOW))

    def test_current_summary_recomputes_scores_and_excludes_all_free_text_and_identity(self):
        summary = self.read()
        self.assertEqual(summary['score_percent'], '80.00')
        self.assertEqual(dict(summary['scores']), {name: 4 for name in SCORE_ITEMS})
        self.assertEqual(summary['status'], 'CURRENT')
        context = observe_combi_batch(self.rows, self.raw, 'RICE', now=NOW).model_context()
        text = json.dumps(context)
        self.assertNotIn('private-actor', text)
        self.assertNotIn('private-note', text)
        self.assertNotIn('synthetic observation', text)
        self.assertNotIn('completed_at', text)
        self.assertEqual(context['quality']['scores']['taste'], 4)

    def test_missing_invalidated_or_safety_blocked_pointer_is_not_restored(self):
        self.pointer['usable'] = False
        self.assertEqual(self.read(), {'status': 'UNAVAILABLE'})
        observed = observe_combi_batch(self.rows, self.raw, 'RICE', now=NOW)
        self.assertIn('CURRENT_QUALITY_UNAVAILABLE', observed.unresolved)
        self.step['safety_status'] = 'FAILED'
        self.assertEqual(self.read(), {'status': 'BLOCKED_BY_SAFETY'})
        self.step['safety_status'] = 'PASSED'
        del self.step['quality_current']
        self.assertEqual(self.read(), {'status': 'UNAVAILABLE'})

    def test_stale_pointer_safety_revision_or_event_rejected(self):
        original = deepcopy(self.rows)
        mutations = [lambda: self.pointer.update(safety_version=2), lambda: self.step.update(quality_version=2),
            lambda: self.pointer.update(event_sk='EVENT#000000000999'),
            lambda: self.event.update(event='MACKEREL_QUALITY_ASSESSED'),
            lambda: self.a.update(version=True), lambda: self.a.update(safety_status='FAILED'),
            lambda: self.a.update(scale_version='other'), lambda: self.a.update(formula_version='other'),
            lambda: self.a.update(equipment_size='other'), lambda: self.a.update(kind='REVISION')]
        for change in mutations:
            with self.subTest(change=change):
                change()
                with self.assertRaises(ValueError): self.read()
                self.rows = deepcopy(original); self.meta = self.rows['META']
                self.step = self.rows['JOB#RICE#' + self.meta['rice_cooking_job_id']]
                self.pointer = self.step['quality_current']; self.event = self.rows[self.pointer['event_sk']]
                self.a = self.event['assessment']

    def test_newer_or_duplicate_completed_version_does_not_leave_old_pointer_current(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.rows['EVENT#000000000012'] = {**deepcopy(self.event),
                    'assessment': {**deepcopy(self.a), 'version': version}}
                with self.assertRaises(ValueError): self.read()

    def test_forged_score_formula_flags_and_menu_reason_rejected(self):
        original = deepcopy(self.a)
        changes = [{'score_total': 35}, {'score_percent': '81'}, {'score_percent': 'NaN'},
            {'quality_target': False}, {'quality_passed': 1}, {'quality_reason_codes': ['DRY_TEXTURE']},
            {'planned_portions': 2}, {'actual_portions': True}, {'overall_satisfaction': 6},
            {'scores': {name: True for name in SCORE_ITEMS}},
            {'scores': {name: Decimal('1E100000') for name in SCORE_ITEMS}},
            {'observations': {name: '' for name in SCORE_ITEMS}}]
        for change in changes:
            with self.subTest(change=change):
                self.a.clear(); self.a.update(deepcopy(original)); self.a.update(change)
                with self.assertRaises((ValueError, ArithmeticError)): self.read()

    def test_high_score_manual_failure_preserves_controlled_reason(self):
        self.a.update(quality_passed=False, quality_target=False, quality_status='QUALITY_FAILED',
                      quality_reason_codes=['RICE_CLUMPED'])
        self.pointer.update(quality_target=False, quality_status='QUALITY_FAILED')
        self.step['quality_status'] = 'QUALITY_FAILED'
        summary = self.read()
        self.assertEqual(summary['score_percent'], '80.00')
        self.assertEqual(summary['quality_status'], 'QUALITY_FAILED')
        self.assertEqual(summary['reason_codes'], ('RICE_CLUMPED',))

    def test_valid_revision_uses_newest_completed_version_without_exposing_revision_reason(self):
        old = deepcopy(self.event)
        new_key = 'EVENT#000000000012'
        new = deepcopy(old)
        new.update(sk=new_key)
        new['assessment'].update(version=2, previous_version=1, kind='REVISION', reason='private-revision-reason')
        self.rows[new_key] = new
        self.step['quality_version'] = 2
        self.pointer.update(version=2, event_sk=new_key)
        result = self.read()
        self.assertEqual(result['version'], 2)
        self.assertNotIn('private-revision-reason', json.dumps(result))

    def test_future_pre_unload_and_mismatched_event_time_rejected(self):
        for at in ('2099-01-01T00:00:00Z', '2000-01-01T00:00:00Z'):
            with self.subTest(at=at):
                self.a['completed_at'] = at
                with self.assertRaises(ValueError): self.read()
        self.a['completed_at'] = iso(NOW)
        with self.assertRaises(ValueError): self.read()

    def test_input_builder_does_not_accept_missing_quality_summary_even_if_unresolved_is_forged_empty(self):
        current = observe_combi_batch(self.rows, self.raw, 'RICE', now=NOW)
        rows, raw = fixture('prior')
        prior = observe_combi_batch(rows, raw, 'RICE', now=NOW)
        for missing in ((), None, (('status', 'UNAVAILABLE'),)):
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError): build_combi_inputs(replace(current, quality=missing), prior)


if __name__ == '__main__':
    unittest.main()
