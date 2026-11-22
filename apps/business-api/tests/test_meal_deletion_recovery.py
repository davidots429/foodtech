import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.meal_deletion_recovery import MealDeletionRecovery


class MealRecoveryTests(unittest.TestCase):
    def test_recovery_checks_before_coordinator(self):
        store = SimpleNamespace(auth_table='auth', registry_table='registry')
        coordinator = SimpleNamespace(store=store, step=Mock(return_value={'phase': 'CAPTURE'}))
        recovery = SimpleNamespace(store=store, use_locked=Mock(side_effect=ValueError('invalid owner')))
        service = MealDeletionRecovery(recovery=recovery, coordinator=coordinator)
        service._writer_quiescence_proven = Mock(return_value=True)
        scope = {'authenticated_subject': str(uuid4()), 'request_id': str(uuid4())}
        with self.assertRaises(ValueError):
            service.step(**scope)
        coordinator.step.assert_not_called()

        def verified(**kwargs):
            kwargs['action'](bytearray(b'private'), bytearray(b'locator'))
            return {'status': 'DELETING'}
        recovery.use_locked.side_effect = verified
        self.assertEqual(service.step(**scope), {'phase': 'CAPTURE'})
        coordinator.step.assert_called_once_with(subject=scope['authenticated_subject'],
                                                 request_id=scope['request_id'], limit=20)

    def test_different_storage_boundaries_are_rejected(self):
        recovery = SimpleNamespace(store=SimpleNamespace(auth_table='auth', registry_table='other'))
        coordinator = SimpleNamespace(store=SimpleNamespace(auth_table='auth', registry_table='registry'))
        with self.assertRaises(ValueError):
            MealDeletionRecovery(recovery=recovery, coordinator=coordinator)

    def test_direct_meal_recovery_is_blocked_without_writer_fencing(self):
        store = SimpleNamespace(auth_table='auth', registry_table='registry')
        coordinator = SimpleNamespace(store=store, step=Mock())
        def verified(**kwargs):
            kwargs['action'](bytearray(b'internal'), bytearray(b'locator'))
            return {'status': 'DELETING'}
        recovery = SimpleNamespace(store=store, use_locked=Mock(side_effect=verified))
        service = MealDeletionRecovery(recovery=recovery, coordinator=coordinator)
        self.assertEqual(service.step(authenticated_subject=str(uuid4()), request_id=str(uuid4())),
                         {'phase': 'QUIESCENCE', 'writer_quiescence_required': True})
        coordinator.step.assert_not_called()


if __name__ == '__main__':
    unittest.main()
