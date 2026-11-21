import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from foodtech_business_api.job_deletion_recovery import JobDeletionRecovery


class JobRecoveryTests(unittest.TestCase):
    def test_only_guarded_recovery_runs_job_step(self):
        store=SimpleNamespace(auth_table='auth',registry_table='registry')
        coordinator=SimpleNamespace(store=store,step=Mock(return_value={'phase':'JOBS'}))
        recovery=SimpleNamespace(store=store,use_locked=Mock())
        service=JobDeletionRecovery(recovery=recovery,coordinator=coordinator)
        scope={'authenticated_subject':str(uuid4()),'request_id':str(uuid4())}
        recovery.use_locked.side_effect=ValueError('lock unavailable')
        with self.assertRaises(ValueError):service.step(**scope)
        coordinator.step.assert_not_called()
        def verify_then_run(**kwargs):
            kwargs['action'](bytearray(b'private'),bytearray(b'locator'))
            return {'status':'DELETING'}
        recovery.use_locked.side_effect=verify_then_run
        self.assertEqual(service.step(**scope),{'phase':'JOBS'})
        coordinator.step.assert_called_once_with(subject=scope['authenticated_subject'],request_id=scope['request_id'],limit=20)

    def test_mismatched_stores_rejected(self):
        recovery=SimpleNamespace(store=SimpleNamespace(auth_table='auth',registry_table='other'))
        coordinator=SimpleNamespace(store=SimpleNamespace(auth_table='auth',registry_table='registry'))
        with self.assertRaises(ValueError):JobDeletionRecovery(recovery=recovery,coordinator=coordinator)


if __name__=='__main__':unittest.main()
