import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from foodtech_business_api.tier_deletion_coordinator import TierDeletionCoordinator
from foodtech_business_api.tier_deletion_recovery import TierDeletionRecovery
from test_tier_deletion_worker import MemoryProgress


class TierCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tier=Mock();self.progress=MemoryProgress();self.cipher=Mock()
        self.progress.auth_table='auth';self.progress.registry_table='registry'
        self.capture=Mock();self.capture.tier_store=self.tier;self.capture.progress_store=self.progress;self.capture.cipher=self.cipher
        import json
        self.capture._json.side_effect=json.loads
        self.capture._load.side_effect=lambda s,r,d:self.progress.load_checkpoint(cipher=self.cipher,subject=s,request_id=r,domain=d)
        self.capture.capture_page.return_value={'complete':True,'captured_count':1}
        self.capture.observe_and_record.side_effect=[{'observation':'PRESENT'},{'observation':'INCONCLUSIVE'},{'observation':'NOT_OBSERVED'}]
        self.worker=SimpleNamespace(index_capture=self.capture,tier_store=self.tier,progress_store=self.progress,cipher=self.cipher,
            run_page=Mock(side_effect=[{'state':'IN_PROGRESS'},{'state':'VERIFYING'}]))
        self.service=TierDeletionCoordinator(capture=self.capture,worker=self.worker)
        self.scope={'subject':str(uuid4()),'request_id':str(uuid4())}

    def test_gsi_present_and_inconclusive_block_progress(self):
        self.assertEqual(self.service.step(**self.scope),{'phase':'DELETE'})
        self.assertEqual(self.service.step(**self.scope),{'phase':'DELETE'})
        self.assertEqual(self.service.step(**self.scope),{'phase':'OBSERVE'})
        self.assertEqual(self.service.step(**self.scope),{'phase':'OBSERVE','observation':'PRESENT'})
        self.assertEqual(self.service.step(**self.scope),{'phase':'OBSERVE','observation':'INCONCLUSIVE'})
        self.assertEqual(self.service.step(**self.scope),{'phase':'VERIFYING'})
        self.assertEqual([x.kwargs['index'] for x in self.capture.observe_and_record.call_args_list],[0,0,0])

    def test_late_gsi_reappearance_reopens_observation(self):
        import json
        key=(self.scope['subject'],self.scope['request_id'],'TIER_COORDINATOR')
        self.progress.items[key]={'version':1,'state':'VERIFYING','checkpoint':json.dumps(
            {'schema':1,'phase':'VERIFYING','index':1,'total':1}).encode()}
        self.capture.observe_and_record.side_effect=None
        self.capture.observe_and_record.return_value={'observation':'PRESENT'}
        self.assertEqual(self.service.recheck_index(**self.scope,index=0),{'phase':'OBSERVE','observation':'PRESENT'})
        self.assertEqual(self.progress.items[key]['state'],'IN_PROGRESS')
        with self.assertRaises(ValueError):self.service.recheck_index(**self.scope,index=0)
        self.capture.observe_and_record.return_value={'observation':'NOT_OBSERVED'}
        self.assertEqual(self.service.step(**self.scope),{'phase':'VERIFYING'})
        self.assertEqual(self.service.recheck_index(**self.scope,index=0),{'phase':'VERIFYING','observation':'NOT_OBSERVED'})

    def test_recovery_failure_prevents_tier_mutation(self):
        recovery=SimpleNamespace(store=SimpleNamespace(auth_table='auth',registry_table='registry'),
                                 use_locked=Mock(side_effect=ValueError('lock unavailable')))
        boundary=TierDeletionRecovery(recovery=recovery,coordinator=self.service)
        with self.assertRaises(ValueError):boundary.step(authenticated_subject=self.scope['subject'],request_id=self.scope['request_id'])
        self.capture.capture_page.assert_not_called();self.worker.run_page.assert_not_called()
        with self.assertRaises(ValueError):boundary.recheck_index(authenticated_subject=self.scope['subject'],request_id=self.scope['request_id'],index=0)
        self.capture.observe_and_record.assert_not_called()

    def test_incorrect_store_rejected(self):
        recovery=SimpleNamespace(store=SimpleNamespace(auth_table='other',registry_table='registry'))
        with self.assertRaises(ValueError):TierDeletionRecovery(recovery=recovery,coordinator=self.service)


if __name__=='__main__':unittest.main()
