import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_business_api.job_deletion_worker import JobDeletionWorker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.source, self.progress, self.cipher = object(), object(), object()
        self.inventory=SimpleNamespace(source_table=self.source,progress_store=self.progress,cipher=self.cipher,
                                       targets=Mock(return_value=iter([SimpleNamespace(job_id='captured')])))
        self.worker=JobDeletionWorker(inventory=self.inventory,source_table=self.source,
                                      progress_store=self.progress,cipher=self.cipher)
        self.scope={'subject':str(uuid4()),'request_id':str(uuid4())}

    def test_only_captured_job_is_passed_to_guarded_partition_cleanup(self):
        with patch('foodtech_business_api.job_deletion_worker.delete_one_job_record',
                   return_value={'state':'META_ONLY','removed':False}) as delete:
            self.assertEqual(self.worker.delete_one(**self.scope,index=0),
                             {'index':0,'state':'META_ONLY','removed':False})
        self.assertEqual(delete.call_args.kwargs['job_id'],'captured')
        self.assertNotIn('captured',repr(delete.call_args.kwargs.get('request_id')))

    def test_missing_target_and_mismatched_dependencies_fail(self):
        self.inventory.targets=Mock(return_value=iter([]))
        with patch('foodtech_business_api.job_deletion_worker.delete_one_job_record') as delete:
            with self.assertRaises(ValueError): self.worker.delete_one(**self.scope,index=0)
            delete.assert_not_called()
        with self.assertRaises(ValueError): JobDeletionWorker(inventory=self.inventory,source_table=object(),
                                                               progress_store=self.progress,cipher=self.cipher)


if __name__ == '__main__': unittest.main()
