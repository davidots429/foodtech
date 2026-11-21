import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from foodtech_ai_gateway.ai_deletion_coordinator import AiDeletionCoordinator
from foodtech_ai_gateway.ai_deletion_recovery import AiDeletionRecovery
from test_tenant_inventory_capture import Progress


class AiCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.source=object();self.progress=Progress();self.cipher=object()
        self.capture=SimpleNamespace(source_table=self.source,progress_store=self.progress,cipher=self.cipher,
            _load=lambda s,r,d:self.progress.load_checkpoint(cipher=self.cipher,subject=s,request_id=r,domain=d),
            _json=json.loads,capture_page=Mock(return_value={'complete':True,'count':2}),
            partitions=lambda **kw:iter([SimpleNamespace(partition='EXEC#one'),SimpleNamespace(partition='CREDENTIAL#one')]))
        self.worker=SimpleNamespace(capture=self.capture,progress_store=self.progress,cipher=self.cipher,source_table=self.source,
                                    run_page=Mock(return_value={'state':'VERIFYING'}))
        self.observer=SimpleNamespace(capture=self.capture,progress_store=self.progress,source_table=self.source,
                                      observe=Mock(side_effect=[{'observation':'BLOCKED'},{'observation':'CLEAR_OBSERVED'}]))
        self.credentials=SimpleNamespace(capture=self.capture,progress_store=self.progress,cipher=self.cipher,source_table=self.source,
                                          observe_readiness=Mock(return_value={'observation':'CLEAR_OBSERVED'}))
        self.coordinator=AiDeletionCoordinator(capture=self.capture,worker=self.worker,observer=self.observer,credentials=self.credentials)
        self.scope={'subject':str(uuid4()),'request_id':str(uuid4())}

    def test_active_execution_blocks_and_credentials_remain_unremoved(self):
        self.assertEqual(self.coordinator.step(**self.scope),{'phase':'PARTITIONS'})
        self.assertEqual(self.coordinator.step(**self.scope),{'phase':'PARTITIONS','execution_blocked':True})
        self.worker.run_page.assert_not_called()
        self.assertEqual(self.coordinator.step(**self.scope),{'phase':'PARTITIONS'})
        self.assertEqual(self.coordinator.step(**self.scope),{'phase':'CREDENTIALS'})
        self.assertEqual(self.coordinator.step(**self.scope),{'phase':'CREDENTIALS'})
        self.assertEqual(self.coordinator.step(**self.scope),{'phase':'CREDENTIALS','credential_settlement_required':True,'execution_blocked':False})
        self.assertEqual(self.worker.run_page.call_count,1)

    def test_recovery_failure_does_not_run_deletion(self):
        self.progress.auth_table='auth';self.progress.registry_table='deletion-registry'
        recovery=SimpleNamespace(store=SimpleNamespace(auth_table='auth',registry_table='deletion-registry'),
                                 use_locked=Mock(side_effect=ValueError('lock unavailable')))
        boundary=AiDeletionRecovery(recovery=recovery,coordinator=self.coordinator)
        with self.assertRaises(ValueError):boundary.step(authenticated_subject=self.scope['subject'],request_id=self.scope['request_id'])
        self.capture.capture_page.assert_not_called();self.worker.run_page.assert_not_called()

    def test_dependencies_must_match(self):
        self.observer.source_table=object()
        with self.assertRaises(ValueError):AiDeletionCoordinator(capture=self.capture,worker=self.worker,
                                                                 observer=self.observer,credentials=self.credentials)


if __name__=='__main__':unittest.main()
