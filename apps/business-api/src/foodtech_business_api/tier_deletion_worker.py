"""Run one bounded, restartable tier-deletion page inside a trusted worker.

The caller must authenticate and authorize the deletion job. Progress storage
and the tier store must enforce the same request lock. This worker does not
verify indexes, backups, other domains, or mark the deletion complete.
"""


class TierDeletionWorker:
    def __init__(self, *, tier_store, progress_store, cipher):
        self.tier_store = tier_store
        self.progress_store = progress_store
        self.cipher = cipher

    def run_page(self, *, subject: str, request_id: str, limit: int) -> dict:
        scope = {'subject': subject, 'request_id': request_id, 'domain': 'TIER'}
        previous = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        if previous is None:
            version = 0
        else:
            if previous['checkpoint'] != b'tier-first-page-v1':
                raise ValueError('invalid tier deletion progress')
            version = previous['version']
        # Always restart at the first remaining key. Every deletion rechecks
        # the lock; if saving progress fails, the page can safely be retried.
        count = self.tier_store.delete_inventory_page(subject, request_id, limit)
        if type(count) is not int or not 0 <= count <= limit:
            raise ValueError('invalid tier deletion page result')
        state = 'VERIFYING' if count == 0 else 'IN_PROGRESS'
        version = self.progress_store.save_checkpoint(cipher=self.cipher, **scope,
            expected_version=version, state=state, checkpoint=b'tier-first-page-v1')
        return {'deleted_count': count, 'version': version, 'state': state}
