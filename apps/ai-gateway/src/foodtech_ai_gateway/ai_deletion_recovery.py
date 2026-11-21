"""Bind AI cleanup to guarded deletion-request recovery, not caller authentication."""


class AiDeletionRecovery:
    def __init__(self, *, recovery, coordinator):
        if recovery.store.auth_table != coordinator.progress.auth_table or \
                recovery.store.registry_table != coordinator.progress.registry_table:
            raise ValueError('AI recovery storage mismatch')
        self.recovery,self.coordinator=recovery,coordinator

    def step(self, *, authenticated_subject, request_id, limit=20):
        result=[]
        def action(_internal_id,_locator):
            result.append(self.coordinator.step(subject=authenticated_subject,request_id=request_id,limit=limit))
        status=self.recovery.use_locked(authenticated_subject=authenticated_subject,request_id=request_id,action=action)
        if status != {'status':'DELETING'} or len(result)!=1:raise ValueError('AI recovery unavailable')
        return result[0]
