import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
from foodtech_business_api.job_deletion_coordinator import JobDeletionCoordinator
from foodtech_business_api.job_deletion_inventory import JobDeletionInventory
from test_profile_deletion_inventory import Progress


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.source=SimpleNamespace(name='cooking',table_arn='arn:source');self.progress=Progress()
        self.cipher=SimpleNamespace(source_table_arn='arn:source')
        self.jobs=JobDeletionInventory(source_table=self.source,progress_store=self.progress,cipher=self.cipher)
        self.jobs.capture_page=Mock(return_value={'complete':True,'count':1})
        self.worker=SimpleNamespace(inventory=self.jobs,delete_one=Mock(side_effect=[
            {'state':'IN_PROGRESS','removed':True},{'state':'META_ONLY','removed':False}]))
        self.bindings=SimpleNamespace(source_table=self.source,progress_store=self.progress,cipher=self.cipher,
            capture_page=Mock(return_value={'complete':True,'count':1}),remove_one=Mock(return_value={'state':'VERIFYING'}))
        self.coordinator=JobDeletionCoordinator(jobs=self.jobs,worker=self.worker,bindings=self.bindings)
        self.scope={'subject':str(uuid4()),'request_id':str(uuid4())}

    def test_order_and_meta_only_is_not_whole_deletion(self):
        with patch('foodtech_business_api.job_deletion_coordinator.clean_one_job_listing',
                   side_effect=[{'state':'IN_PROGRESS','removed':True},{'state':'VERIFYING','removed':False}]):
            results=[self.coordinator.step(**self.scope)['phase'] for _ in range(8)]
        self.assertEqual(results,['RECORDS','RECORDS','BINDINGS','REMOVE_BINDINGS','LISTINGS','LISTINGS','VERIFYING','VERIFYING'])
        self.assertEqual([c.kwargs['index'] for c in self.worker.delete_one.call_args_list],[0,0])
        self.bindings.remove_one.assert_called_once()

    def test_failed_progress_write_repeats_same_binding_safely(self):
        self.coordinator.step(**self.scope);self.coordinator.step(**self.scope);self.coordinator.step(**self.scope)
        self.coordinator.step(**self.scope)
        save=self.progress.save_checkpoint;self.progress.save_checkpoint=Mock(side_effect=RuntimeError('lost'))
        with self.assertRaises(RuntimeError):self.coordinator.step(**self.scope)
        self.progress.save_checkpoint=save
        self.assertEqual(self.coordinator.step(**self.scope),{'phase':'LISTINGS'})
        self.assertEqual(self.bindings.remove_one.call_count,2)

    def test_dependency_mismatch_fails(self):
        self.bindings.cipher=object()
        with self.assertRaises(ValueError):JobDeletionCoordinator(jobs=self.jobs,worker=self.worker,bindings=self.bindings)


if __name__=='__main__':unittest.main()
