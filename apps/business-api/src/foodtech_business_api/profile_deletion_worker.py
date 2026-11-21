"""Delete a captured profile version and record progress, under a deletion lock.

The caller must authenticate the job, stop legacy writers and verify residuals
across all stores. VERIFYING is not a deletion completion receipt.
"""
import json

from .condition_profile_deletion import delete_profile_pair


class ProfileDeletionWorker:
    def __init__(self, *, inventory, progress_store, cipher, source_table):
        if inventory.source_table is not source_table or inventory.progress_store is not progress_store or \
                inventory.cipher is not cipher:
            raise ValueError('profile deletion dependencies do not match inventory')
        self.inventory, self.progress_store, self.cipher, self.source_table = inventory, progress_store, cipher, source_table

    def delete_one(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid profile deletion index')
        target = next((target for n, target in enumerate(self.inventory.targets(
            subject=subject, request_id=request_id)) if n == index), None)
        if target is None:
            raise ValueError('profile deletion target unavailable')
        scope = {'subject': subject, 'request_id': request_id, 'domain': f'PROFILE_DELETE_{index:012d}'}
        raw = json.dumps({'schema': 1, 'profile_id': target.profile_id, 'version': target.version},
                         ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        previous = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        if previous is not None and (previous.get('version') != 1 or previous.get('state') != 'VERIFYING' or
                                     previous.get('checkpoint') != raw):
            raise ValueError('profile deletion progress does not match target')
        delete_profile_pair(self.source_table, self.progress_store.client,
            auth_table=self.progress_store.auth_table, subject=subject, request_id=request_id,
            profile_id=target.profile_id, version=target.version)
        if previous is None:
            self.progress_store.save_checkpoint(cipher=self.cipher, **scope, expected_version=0,
                                                state='VERIFYING', checkpoint=raw)
        return {'index': index, 'state': 'VERIFYING'}
