import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_business_api.meal_deletion_coordinator import MealDeletionCoordinator
from test_profile_deletion_inventory import Progress


class MealCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.source = SimpleNamespace(name='cooking', table_arn='arn:source')
        self.store, self.cipher = Progress(), SimpleNamespace(source_table_arn='arn:source')
        self.meals = SimpleNamespace(source_table=self.source, progress_store=self.store, cipher=self.cipher,
            _json=json.loads, _load=self.load, capture_page=Mock(return_value={'complete': True, 'count': 1}))
        self.worker = SimpleNamespace(inventory=self.meals,
            delete_records=Mock(return_value={'state': 'META_ONLY', 'removed': False}))
        self.residual = SimpleNamespace(DOMAIN='MEAL_RESIDUAL', source_table=self.source,
            progress_store=self.store, cipher=self.cipher,
            observe_page=Mock(return_value={'scan_complete': True, 'residual_found': False}))
        self.final = SimpleNamespace(DOMAIN='MEAL_FINAL_RESIDUAL', source_table=self.source,
            progress_store=self.store, cipher=self.cipher,
            observe_page=Mock(return_value={'scan_complete': True, 'residual_found': False}))
        self.meta = SimpleNamespace(inventory=self.meals, residual=self.residual,
                                    remove_batch=Mock(side_effect=lambda **kw: {'state': 'VERIFYING', 'count': kw['count']}))
        self.sources = SimpleNamespace(inventory=self.meals, inspect_batch=Mock(
            side_effect=lambda **kw: {'state': 'NO_BOUND_SOURCE', 'count': kw['count']}))
        self.coordinator = MealDeletionCoordinator(meals=self.meals, worker=self.worker, residual=self.residual,
            metadata=self.meta, final_residual=self.final, sources=self.sources)
        self.scope = {'subject': self.subject, 'request_id': self.request}

    def load(self, subject, request, domain):
        return self.store.items.get((subject, request, domain))

    def test_runs_each_stage_and_does_not_declare_completion(self):
        with patch('foodtech_business_api.meal_deletion_coordinator.clean_one_meal_listing',
                   return_value={'state': 'VERIFYING', 'removed': False}):
            phases = [self.coordinator.step(**self.scope)['phase'] for _ in range(7)]
            self.assertEqual(self.coordinator.step(**self.scope), {'phase': 'VERIFYING'})
        self.assertEqual(phases, ['RECORDS', 'RESIDUAL', 'META', 'LISTINGS', 'FINAL', 'SOURCES', 'VERIFYING'])

    def test_residual_and_source_ownership_block_forward_progress(self):
        self.residual.observe_page.return_value = {'scan_complete': False, 'residual_found': True}
        self.coordinator.step(**self.scope); self.coordinator.step(**self.scope)
        self.assertEqual(self.coordinator.step(**self.scope), {'phase': 'RESIDUAL', 'residual_found': True})
        self.meta.remove_batch.assert_not_called()
        self.residual.observe_page.return_value = {'scan_complete': True, 'residual_found': False}
        self.sources.inspect_batch.side_effect = None
        self.sources.inspect_batch.return_value = {'state': 'SOURCE_OWNERSHIP_REQUIRED'}
        with patch('foodtech_business_api.meal_deletion_coordinator.clean_one_meal_listing',
                   return_value={'state': 'VERIFYING', 'removed': False}):
            for _ in range(4): self.coordinator.step(**self.scope)
            self.assertEqual(self.coordinator.step(**self.scope), {'phase': 'SOURCES', 'source_ownership_required': True})

    def test_resumed_final_source_or_verifying_checkpoint_rechecks_listing_residuals(self):
        for phase in ('FINAL', 'SOURCES', 'VERIFYING'):
            with self.subTest(phase=phase):
                self.store.items[(self.subject, self.request, 'MEAL_COORDINATOR')] = {
                    'version': 1, 'state': 'VERIFYING' if phase == 'VERIFYING' else 'IN_PROGRESS',
                    'checkpoint': json.dumps({'schema': 1, 'phase': phase, 'index': 1, 'total': 1}).encode('ascii')}
                self.sources.inspect_batch.reset_mock(); self.final.observe_page.reset_mock()
                with patch('foodtech_business_api.meal_deletion_coordinator.clean_one_meal_listing',
                           return_value={'state': 'IN_PROGRESS', 'removed': True}) as clean:
                    self.assertEqual(self.coordinator.step(**self.scope), {'phase': 'LISTINGS'})
                    clean.assert_called_once()
                self.sources.inspect_batch.assert_not_called(); self.final.observe_page.assert_not_called()
                saved = self.store.items[(self.subject, self.request, 'MEAL_COORDINATOR')]
                self.assertEqual(saved['state'], 'IN_PROGRESS')
                self.assertEqual(json.loads(saved['checkpoint'])['phase'], 'LISTINGS')

    def test_final_check_domain_must_be_distinct(self):
        with self.assertRaises(ValueError):
            MealDeletionCoordinator(meals=self.meals, worker=self.worker, residual=self.residual,
                metadata=self.meta, final_residual=self.residual, sources=self.sources)


if __name__ == '__main__':
    unittest.main()
