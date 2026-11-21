"""Restartable deletion of a captured noncredential AI partition.

Caller authentication, stopping legacy writers, active-call reconciliation,
credential-vault deletion, all-domain residual checks and backup recovery are
separate prerequisites. VERIFYING never means customer deletion is complete.
"""
import json

from .legacy_tenant_inventory import delete_registry_key, delete_registry_partition_page


class TenantPartitionWorker:
    def __init__(self, *, capture, progress_store, cipher, source_table):
        if capture.progress_store is not progress_store or capture.cipher is not cipher or \
                capture.source_table is not source_table:
            raise ValueError('AI deletion dependencies do not match inventory')
        self.capture, self.progress_store, self.cipher, self.source_table = capture, progress_store, cipher, source_table

    def run_page(self, *, subject, request_id, index, limit):
        if type(index) is not int or not 0 <= index < 10**12 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid AI deletion page scope')
        target = next((target for number, target in enumerate(self.capture.partitions(
            subject=subject, request_id=request_id)) if number == index), None)
        if target is None:
            raise ValueError('AI deletion target unavailable')
        partition = target.partition
        if partition.startswith('CREDENTIAL#'):
            raise ValueError('credential requires paired vault deletion')
        scope = {'subject': subject, 'request_id': request_id, 'domain': f'AI_DELETE_{index:012d}'}
        previous = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        if previous is None:
            version, cursor = 0, None
        else:
            try:
                state = json.loads(previous['checkpoint'])
                if set(state) != {'schema', 'partition', 'cursor'} or state['schema'] != 1 or \
                        state['partition'] != partition or (state['cursor'] is not None and
                        (not isinstance(state['cursor'], str) or not 1 <= len(state['cursor']) <= 2048)) or \
                        previous['state'] not in {'IN_PROGRESS', 'VERIFYING'}:
                    raise ValueError
                version, cursor = previous['version'], state['cursor']
            except (TypeError, KeyError, ValueError, UnicodeError):
                raise ValueError('invalid AI deletion progress') from None
        if type(version) is not int or not 0 <= version < 2**63 - 1:
            raise ValueError('invalid AI deletion progress')
        owner = self.source_table.get_item(Key={'pk': partition, 'sk': 'META'}, ConsistentRead=True).get('Item')
        if owner is None:
            # Also checks there are no child rows and no active execution marker.
            delete_registry_key(self.source_table, self.progress_store.client,
                auth_table=self.progress_store.auth_table, deletion_registry_table=self.progress_store.registry_table, tenant=subject, request_id=request_id,
                partition=partition, sk='META')
            result = {'deleted_children': 0, 'next_cursor': None, 'partition_removed': True}
        else:
            result = delete_registry_partition_page(self.source_table, self.progress_store.client,
                auth_table=self.progress_store.auth_table, deletion_registry_table=self.progress_store.registry_table, tenant=subject, request_id=request_id,
                partition=partition, limit=limit, cursor=cursor)
        state = 'VERIFYING' if result['partition_removed'] else 'IN_PROGRESS'
        raw = json.dumps({'schema': 1, 'partition': partition, 'cursor': result['next_cursor']},
                         separators=(',', ':'), sort_keys=True).encode('utf-8')
        saved = self.progress_store.save_checkpoint(cipher=self.cipher, **scope, expected_version=version,
                                                    state=state, checkpoint=raw)
        return {'index': index, 'version': saved, 'state': state, 'deleted_children': result['deleted_children']}
