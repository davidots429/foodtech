import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.profile_deletion_recovery import ProfileDeletionRecovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = SimpleNamespace(auth_table='auth', registry_table='registry')
        self.coordinator = SimpleNamespace(store=self.store, step=Mock(return_value={'phase':'CAPTURE','residual_found':False}))
        self.recovery = SimpleNamespace(store=self.store, use_locked=Mock())
        self.scope = {'authenticated_subject':str(uuid4()), 'request_id':str(uuid4())}
        self.adapter = ProfileDeletionRecovery(recovery=self.recovery, coordinator=self.coordinator)

    def test_runs_one_step_only_after_recovery_check(self):
        self.adapter._writer_quiescence_proven=Mock(return_value=True)
        def locked(**kwargs):
            kwargs['action'](bytearray(b'internal-secret'), bytearray(b'locator-secret'))
            return {'status':'DELETING'}
        self.recovery.use_locked.side_effect=locked
        result=self.adapter.step(**self.scope)
        self.assertEqual(result, {'phase':'CAPTURE','residual_found':False})
        self.assertNotIn('secret', repr(result))
        self.coordinator.step.assert_called_once_with(subject=self.scope['authenticated_subject'],
                                                     request_id=self.scope['request_id'], limit=20)

    def test_verified_request_still_blocks_without_writer_fence(self):
        def locked(**kwargs):
            kwargs['action'](bytearray(b'internal'), bytearray(b'locator'))
            return {'status': 'DELETING'}
        self.recovery.use_locked.side_effect = locked
        self.assertEqual(self.adapter.step(**self.scope),
                         {'phase': 'QUIESCENCE', 'writer_quiescence_required': True})
        self.coordinator.step.assert_not_called()

    def test_failed_recovery_never_runs_step(self):
        self.recovery.use_locked.side_effect=ValueError('unavailable')
        with self.assertRaises(ValueError): self.adapter.step(**self.scope)
        self.coordinator.step.assert_not_called()
        self.recovery.use_locked.side_effect=None
        self.recovery.use_locked.return_value={'status':'DELETING'}
        with self.assertRaises(ValueError): self.adapter.step(**self.scope)
        self.coordinator.step.assert_not_called()

    def test_mismatched_stores_are_rejected(self):
        self.recovery.store=SimpleNamespace(auth_table='other',registry_table='registry')
        with self.assertRaises(ValueError): ProfileDeletionRecovery(recovery=self.recovery,coordinator=self.coordinator)


if __name__ == '__main__': unittest.main()
