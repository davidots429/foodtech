"""Run one locked cleanup step for a verified captured cooking job.

No job is declared deleted here. META, indexes, source bindings and other
stores remain until separately verified and handled by the full saga.
"""
from .job_partition_deletion import delete_one_job_record
from .job_binding_deletion import delete_job_binding


class JobDeletionWorker:
    def __init__(self, *, inventory, source_table, progress_store, cipher):
        if inventory.source_table is not source_table or inventory.progress_store is not progress_store or \
                inventory.cipher is not cipher:
            raise ValueError('job deletion dependencies do not match')
        self.inventory, self.source_table = inventory, source_table
        self.progress_store, self.cipher = progress_store, cipher

    def _target(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid job deletion index')
        target = next((target for n, target in enumerate(self.inventory.targets(
            subject=subject, request_id=request_id)) if n == index), None)
        if target is None:
            raise ValueError('job deletion target unavailable')
        return target

    def delete_one(self, *, subject, request_id, index):
        target=self._target(subject=subject,request_id=request_id,index=index)
        result=delete_one_job_record(source_table=self.source_table, progress_store=self.progress_store,
            cipher=self.cipher, subject=subject, request_id=request_id, job_id=target.job_id)
        return {'index': index, 'state': result['state'], 'removed': result['removed']}

    def remove_binding(self, *, subject, request_id, index):
        target=self._target(subject=subject,request_id=request_id,index=index)
        result=delete_job_binding(source_table=self.source_table,progress_store=self.progress_store,
            cipher=self.cipher,subject=subject,request_id=request_id,job_id=target.job_id)
        return {'index':index,'state':result['state']}
