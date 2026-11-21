"""Observe remaining subject-owned reverse bindings, not deletion completion."""
from .job_binding_inventory import JobBindingInventory
from .profile_deletion_residual import ProfileResidualScan


class JobBindingResidualScan(ProfileResidualScan):
    DOMAIN='JOB_BINDING_RESIDUAL'

    def __init__(self, *, source_table, progress_store, cipher):
        super().__init__(source_table=source_table,progress_store=progress_store,cipher=cipher)
        self.binding_inventory=JobBindingInventory(source_table=source_table,progress_store=progress_store,cipher=cipher)

    def _scan(self, *, subject, limit, checkpoint):
        return self.binding_inventory._scan(subject=subject,limit=min(limit,20),checkpoint=checkpoint)

    def repair_one(self, **_kwargs):
        raise ValueError('binding residual repair requires separate ownership verification')


class JobBindingFinalResidualScan(JobBindingResidualScan):
    DOMAIN='JOB_BINDING_FINAL_RESIDUAL'
