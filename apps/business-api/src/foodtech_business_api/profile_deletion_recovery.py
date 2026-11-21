"""Bind one profile deletion step to a verified locked recovery request.

This internal adapter is not caller authentication: only a separately
identified and authorized maintenance worker may invoke it. The caller must
ensure legacy writers are stopped and verify all stores before completion.
"""


class ProfileDeletionRecovery:
    def __init__(self, *, recovery, coordinator):
        recovery_store = recovery.store
        progress = coordinator.store
        if recovery_store.auth_table != progress.auth_table or \
                recovery_store.registry_table != progress.registry_table:
            raise ValueError('deletion recovery storage mismatch')
        self.recovery, self.coordinator = recovery, coordinator

    def step(self, *, authenticated_subject, request_id, limit=20):
        result = []
        def action(_internal_id, _capsule_locator):
            result.append(self.coordinator.step(subject=authenticated_subject, request_id=request_id, limit=limit))
        status = self.recovery.use_locked(authenticated_subject=authenticated_subject,
                                          request_id=request_id, action=action)
        if status != {'status': 'DELETING'} or len(result) != 1:
            raise ValueError('profile deletion recovery unavailable')
        return result[0]
