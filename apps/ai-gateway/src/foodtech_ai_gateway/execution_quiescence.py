"""Observe captured AI execution state under an existing deletion lock.

This does not stop writers or prove no external call remains in flight. The
caller must independently quiesce old writers and reconcile all reservations.
"""
import re

from boto3.dynamodb.conditions import Key
from .legacy_tenant_inventory import observe_active_markers


class ExecutionQuiescence:
    def __init__(self, *, capture, progress_store, cipher, source_table):
        if capture.source_table is not source_table or capture.progress_store is not progress_store or \
                capture.cipher is not cipher:
            raise ValueError('execution observation dependencies do not match inventory')
        self.capture, self.progress_store, self.source_table = capture, progress_store, source_table

    def observe(self, *, subject, request_id):
        def markers():
            return observe_active_markers(self.source_table, self.progress_store.client,
                auth_table=self.progress_store.auth_table, tenant=subject, request_id=request_id)
        initial = markers()
        if initial not in {'PRESENT', 'CLEAR'}:
            raise ValueError('invalid active marker observation')
        if initial == 'PRESENT':
            return {'observation': 'BLOCKED', 'reason': 'ACTIVE_MARKER'}
        checked = 0
        for target in self.capture.partitions(subject=subject, request_id=request_id):
            partition = target.partition
            if not partition.startswith('EXEC#'):
                continue
            if not re.fullmatch(r'EXEC#[A-Za-z0-9_-]{1,128}', partition):
                raise ValueError('invalid execution inventory target')
            meta = self.source_table.get_item(Key={'pk': partition, 'sk': 'META'}, ConsistentRead=True).get('Item')
            if meta is None:
                page = self.source_table.query(KeyConditionExpression=Key('pk').eq(partition),
                                               ProjectionExpression='pk, sk', ConsistentRead=True, Limit=1)
                if not isinstance(page.get('Items'), list) or page['Items'] or page.get('LastEvaluatedKey'):
                    raise ValueError('execution inventory has orphan records')
            else:
                if meta.get('pk') != partition or meta.get('sk') != 'META' or meta.get('tenant_id') != subject or \
                        meta.get('state') not in {'PENDING', 'RESERVED', 'SUCCEEDED', 'FAILED'}:
                    raise ValueError('invalid execution inventory state')
                if meta['state'] == 'RESERVED':
                    return {'observation': 'BLOCKED', 'reason': 'RESERVED_EXECUTION'}
            checked += 1
        final = markers()
        if final not in {'PRESENT', 'CLEAR'}:
            raise ValueError('invalid active marker observation')
        if final == 'PRESENT':
            return {'observation': 'BLOCKED', 'reason': 'ACTIVE_MARKER'}
        return {'observation': 'CLEAR_OBSERVED', 'checked_executions': checked}
