"""Execute one tier cleanup step only after guarded locked-request recovery.

External caller authentication, a JIT role and full Saga dispatch are separate.
"""


class TierDeletionRecovery:
    def __init__(self, *, recovery, coordinator):
        if recovery.store.auth_table != coordinator.progress.auth_table or \
                recovery.store.registry_table != coordinator.progress.registry_table:
            raise ValueError('tier recovery storage mismatch')
        self.recovery,self.coordinator=recovery,coordinator

    def step(self, *, authenticated_subject, request_id, limit=20):
        result=[]
        def action(_user,_locator):
            result.append(self.coordinator.step(subject=authenticated_subject,request_id=request_id,limit=limit))
        status=self.recovery.use_locked(authenticated_subject=authenticated_subject,request_id=request_id,action=action)
        if status != {'status':'DELETING'} or len(result)!=1:raise ValueError('tier recovery unavailable')
        return result[0]

    def recheck_index(self, *, authenticated_subject, request_id, index):
        result=[]
        def action(_user,_locator):
            result.append(self.coordinator.recheck_index(subject=authenticated_subject,request_id=request_id,index=index))
        status=self.recovery.use_locked(authenticated_subject=authenticated_subject,request_id=request_id,action=action)
        if status != {'status':'DELETING'} or len(result)!=1:raise ValueError('tier recovery unavailable')
        return result[0]
