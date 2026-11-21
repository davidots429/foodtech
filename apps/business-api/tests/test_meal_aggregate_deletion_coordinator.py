import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_business_api.meal_aggregate_deletion_coordinator import MealAggregateDeletionCoordinator
from test_profile_deletion_inventory import Progress


class MealAggregateCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.store, self.cipher, self.source = Progress(), object(), object()
        self.inventory = SimpleNamespace(progress_store=self.store, cipher=self.cipher, source_table=self.source,
            _json=json.loads, _load=lambda s, r, d: self.store.items.get((s, r, d)),
            capture_page=Mock(return_value={'complete': True, 'count': 1}))
        self.worker = SimpleNamespace(inventory=self.inventory, delete_batch=Mock(return_value={'state': 'VERIFYING', 'count': 1}))
        self.residual = SimpleNamespace(source_table=self.source, progress_store=self.store, cipher=self.cipher,
            DOMAIN='MEAL_AGGREGATE_RESIDUAL', observe_page=Mock(return_value={'scan_complete': True, 'residual_found': False}))
        self.service = MealAggregateDeletionCoordinator(inventory=self.inventory, worker=self.worker, residual=self.residual)
        self.scope = {'subject': self.subject, 'request_id': self.request}

    def test_no_fence_blocks_even_old_verifying_checkpoint(self):
        key = (self.subject, self.request, 'MEAL_AGGREGATE_COORDINATOR')
        self.store.items[key] = {'version': 1, 'state': 'VERIFYING',
            'checkpoint': json.dumps({'schema': 1, 'phase': 'VERIFYING', 'index': 1, 'total': 1}).encode()}
        self.assertEqual(self.service.step(**self.scope), {'phase': 'QUIESCENCE', 'writer_quiescence_required': True})
        self.worker.delete_batch.assert_not_called()

    def test_steps_and_residual_block_with_simulated_fence(self):
        self.service._writer_quiescence_proven = Mock(return_value=True)
        with patch('foodtech_business_api.meal_aggregate_deletion_coordinator.clean_one_meal_aggregate_group',
                   return_value={'state': 'VERIFYING', 'removed': False}):
            self.assertEqual(self.service.step(**self.scope), {'phase': 'DELETE'})
            self.assertEqual(self.service.step(**self.scope), {'phase': 'GROUPS'})
            self.assertEqual(self.service.step(**self.scope), {'phase': 'RESIDUAL'})
            self.residual.observe_page.return_value = {'scan_complete': False, 'residual_found': True}
            self.assertEqual(self.service.step(**self.scope), {'phase': 'RESIDUAL', 'residual_found': True})
            self.residual.observe_page.return_value = {'scan_complete': True, 'residual_found': False}
            self.assertEqual(self.service.step(**self.scope), {'phase': 'FINAL_GROUPS'})
            self.assertEqual(self.service.step(**self.scope), {'phase': 'VERIFYING'})

    def test_delete_step_advances_only_after_complete_bounded_batch(self):
        self.service._writer_quiescence_proven = Mock(return_value=True)
        key = (self.subject, self.request, 'MEAL_AGGREGATE_COORDINATOR')
        self.store.items[key] = {'version': 1, 'state': 'IN_PROGRESS',
            'checkpoint': json.dumps({'schema': 1, 'phase': 'DELETE', 'index': 0, 'total': 3}).encode()}
        self.worker.delete_batch.return_value = {'state': 'VERIFYING', 'count': 2}
        self.assertEqual(self.service.step(**self.scope, limit=2), {'phase': 'DELETE'})
        self.worker.delete_batch.assert_called_once_with(**self.scope, start=0, count=2)
        self.assertEqual(json.loads(self.store.items[key]['checkpoint'])['index'], 2)
        self.worker.delete_batch.return_value = {'state': 'VERIFYING', 'count': 0}
        with self.assertRaises(ValueError):
            self.service.step(**self.scope, limit=2)
        self.assertEqual(json.loads(self.store.items[key]['checkpoint'])['index'], 2)


if __name__ == '__main__':
    unittest.main()
