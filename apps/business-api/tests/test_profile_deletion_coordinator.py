import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_business_api.profile_deletion_coordinator import ProfileDeletionCoordinator
from foodtech_business_api.profile_deletion_inventory import ProfileDeletionInventory
from test_profile_deletion_inventory import Progress


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.source=SimpleNamespace(name='cooking',table_arn='arn:source')
        self.store=Progress(); self.cipher=SimpleNamespace(source_table_arn='arn:source')
        self.inventory=ProfileDeletionInventory(source_table=self.source,progress_store=self.store,cipher=self.cipher)
        self.inventory.capture_page=Mock(side_effect=[{'complete':False},{'complete':True,'count':2}])
        self.worker=SimpleNamespace(inventory=self.inventory,delete_one=Mock())
        self.residual=SimpleNamespace(source_table=self.source,progress_store=self.store,cipher=self.cipher,
            observe_page=Mock(side_effect=[{'residual_found':True,'scan_complete':False},
                                          {'residual_found':False,'scan_complete':True}]))
        self.coordinator=ProfileDeletionCoordinator(inventory=self.inventory,worker=self.worker,residual=self.residual)
        self.scope={'subject':str(uuid4()),'request_id':str(uuid4())}

    def test_order_residual_retry_and_no_completion_claim(self):
        with patch('foodtech_business_api.profile_deletion_coordinator.clean_one_orphan',
                   side_effect=[{'removed':True},{'removed':False},{'removed':True},{'removed':False}]) as orphan:
            states=[self.coordinator.step(**self.scope) for _ in range(11)]
        self.assertEqual([s['phase'] for s in states],
                         ['CAPTURE','DELETE','DELETE','ORPHANS','ORPHANS','RESIDUAL','RESIDUAL','FINAL_LISTING','FINAL_LISTING','VERIFYING','VERIFYING'])
        self.assertTrue(states[6]['residual_found'])
        self.assertEqual([x.kwargs['index'] for x in self.worker.delete_one.call_args_list],[0,1])
        self.assertEqual(orphan.call_count,4)
        self.assertEqual(self.residual.observe_page.call_count,2)

    def test_failed_checkpoint_retries_same_delete(self):
        self.inventory.capture_page=Mock(return_value={'complete':True,'count':1})
        self.coordinator.step(**self.scope)
        save=self.store.save_checkpoint
        self.store.save_checkpoint=Mock(side_effect=RuntimeError('write lost'))
        with self.assertRaises(RuntimeError): self.coordinator.step(**self.scope)
        self.store.save_checkpoint=save
        self.coordinator.step(**self.scope)
        self.assertEqual([x.kwargs['index'] for x in self.worker.delete_one.call_args_list],[0,0])

    def test_mismatched_dependencies_fail(self):
        self.residual.cipher=object()
        with self.assertRaises(ValueError): ProfileDeletionCoordinator(inventory=self.inventory,worker=self.worker,residual=self.residual)


if __name__ == '__main__': unittest.main()
