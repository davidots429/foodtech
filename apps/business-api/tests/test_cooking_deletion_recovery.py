import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.cooking_deletion_recovery import CookingDeletionRecovery
from test_profile_deletion_inventory import Progress


class CookingDeletionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store, self.cipher = Progress(), object()
        self.scope = {'authenticated_subject': str(uuid4()), 'request_id': str(uuid4())}
        self.profile, self.job, self.meal = (self.make_recovery() for _ in range(3))
        self.service = CookingDeletionRecovery(profile=self.profile, job=self.job, meal=self.meal,
                                               progress_store=self.store, cipher=self.cipher)

    def make_recovery(self):
        coordinator = SimpleNamespace(store=self.store, cipher=self.cipher)
        recovery = SimpleNamespace(store=SimpleNamespace(auth_table='auth', registry_table='registry'))
        return SimpleNamespace(coordinator=coordinator, recovery=recovery,
                               step=Mock(return_value={'phase': 'VERIFYING'}))

    def test_each_guarded_domain_runs_in_order_and_stops_at_verifying(self):
        self.service._writer_quiescence_proven = Mock(return_value=True)
        self.assertEqual(self.service.step(**self.scope), {'phase': 'JOB'})
        self.assertEqual(self.service.step(**self.scope), {'phase': 'MEAL'})
        self.assertEqual(self.service.step(**self.scope), {'phase': 'VERIFYING'})
        self.assertEqual(self.service.step(**self.scope), {'phase': 'VERIFYING'})
        for domain in (self.profile, self.job, self.meal):
            domain.step.assert_called_once_with(**self.scope, limit=20)

    def test_blocked_domain_does_not_advance(self):
        self.service._writer_quiescence_proven = Mock(return_value=True)
        self.profile.step.return_value = {'phase': 'RESIDUAL', 'residual_found': True}
        self.assertEqual(self.service.step(**self.scope),
                         {'phase': 'PROFILE', 'domain_phase': 'RESIDUAL', 'residual_found': True})
        self.job.step.assert_not_called()
        self.profile.step.return_value = {'phase': 'VERIFYING'}
        self.service.step(**self.scope)
        self.job.step.return_value = {'phase': 'SOURCES', 'source_ownership_required': True}
        self.assertEqual(self.service.step(**self.scope),
            {'phase': 'JOB', 'domain_phase': 'SOURCES', 'source_ownership_required': True})
        self.meal.step.assert_not_called()

    def test_absent_writer_fence_blocks_even_previously_verifying_checkpoint(self):
        for phase in ('PROFILE', 'JOB', 'MEAL', 'VERIFYING'):
            key = (self.scope['authenticated_subject'], self.scope['request_id'], 'COOKING_RECOVERY')
            self.store.items[key] = {'version': 1, 'state': 'VERIFYING' if phase == 'VERIFYING' else 'IN_PROGRESS',
                                     'checkpoint': json.dumps({'schema': 1, 'phase': phase}).encode()}
            self.assertEqual(self.service.step(**self.scope),
                             {'phase': 'QUIESCENCE', 'writer_quiescence_required': True})
        for domain in (self.profile, self.job, self.meal):
            domain.step.assert_not_called()

    def test_mismatched_dependencies_and_bad_checkpoint_fail(self):
        self.meal.coordinator.cipher = object()
        with self.assertRaises(ValueError):
            CookingDeletionRecovery(profile=self.profile, job=self.job, meal=self.meal,
                                    progress_store=self.store, cipher=self.cipher)
        self.meal.coordinator.cipher = self.cipher
        key = (self.scope['authenticated_subject'], self.scope['request_id'], 'COOKING_RECOVERY')
        self.store.items[key] = {'version': 1, 'state': 'VERIFYING',
                                 'checkpoint': json.dumps({'schema': 1, 'phase': 'JOB'}).encode()}
        with self.assertRaises(ValueError):
            self.service.step(**self.scope)
        self.job.step.assert_not_called()


if __name__ == '__main__':
    unittest.main()
