"""Preserve a meal META snapshot before guarded removal.

Requires stopped writers and a completed independent non-META residual scan.
The protected snapshot is retained for downstream source review. This operation
does not establish ownership of source data or account-deletion completion.
"""
import json
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .condition_profile_deletion import ProfileDeletionConflict
from .job_meta_deletion import JobMetaDeletion


class MealMetaDeletion:
    def __init__(self, *, inventory, residual):
        if getattr(residual, 'DOMAIN', None) != 'MEAL_RESIDUAL' or \
                residual.source_table is not inventory.source_table or residual.progress_store is not inventory.progress_store or \
                residual.cipher is not inventory.cipher:
            raise ValueError('meal metadata dependencies do not match')
        self.inventory, self.residual = inventory, residual
        self.source_table, self.progress_store, self.cipher = inventory.source_table, inventory.progress_store, inventory.cipher

    def remove_one(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid meal metadata index')
        state, _ = self.residual._checkpoint(subject, request_id)
        if not state['complete'] or state['residual']:
            raise ValueError('meal residual check incomplete')
        target = next((item for n, item in enumerate(self.inventory.targets(
            subject=subject, request_id=request_id)) if n == index), None)
        if target is None:
            raise ValueError('meal metadata target unavailable')
        scope = {'subject': subject, 'request_id': request_id, 'domain': f'MEAL_META_{index:012d}'}
        saved = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{target.meal_id}'
        page = self.source_table.query(KeyConditionExpression=Key('pk').eq(pk), ConsistentRead=True, Limit=2)
        rows = page.get('Items')
        if not isinstance(rows, list) or len(rows) > 1 or page.get('LastEvaluatedKey') is not None or \
                (rows and (not isinstance(rows[0], dict) or rows[0].get('pk') != pk or rows[0].get('sk') != 'META')):
            raise ValueError('meal partition still has records')
        if saved is None:
            if not rows:
                raise ValueError('meal metadata unavailable')
            meta = rows[0]
            version = meta.get('version')
            if meta.get('meal_batch_id') != target.meal_id or isinstance(version, bool) or \
                    not isinstance(version, (int, Decimal)) or version != int(version) or not 1 <= version < 10**18:
                raise ValueError('invalid meal metadata')
            serializer = TypeSerializer()
            encoded = {key: JobMetaDeletion._safe(serializer.serialize(value)) for key, value in meta.items()}
            snapshot = {'schema': 1, 'meal_id': target.meal_id, 'version': int(version), 'metadata': encoded}
            raw = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
            if len(raw) > 16384:
                raise ValueError('meal metadata snapshot too large')
            self.progress_store.save_checkpoint(cipher=self.cipher, **scope, expected_version=0,
                                                state='IN_PROGRESS', checkpoint=raw)
        else:
            if saved.get('version') != 1 or saved.get('state') != 'IN_PROGRESS':
                raise ValueError('invalid meal metadata intent')
            snapshot = self.inventory._json(saved['checkpoint'])
            if set(snapshot) != {'schema', 'meal_id', 'version', 'metadata'} or type(snapshot['schema']) is not int or \
                    snapshot['schema'] != 1 or snapshot['meal_id'] != target.meal_id or \
                    type(snapshot['version']) is not int or not 1 <= snapshot['version'] < 10**18 or \
                    not isinstance(snapshot['metadata'], dict):
                raise ValueError('invalid meal metadata intent')
        serializer = TypeSerializer()

        def encode(values):
            return {key: serializer.serialize(value) for key, value in values.items()}

        try:
            self.progress_store.client.transact_write_items(TransactItems=[
                {'ConditionCheck': {'TableName': self.progress_store.auth_table, 'Key': encode({'pk': f'USER#{subject}'}),
                    'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
                    'ExpressionAttributeValues': encode({':request': request_id, ':yes': True})}},
                {'ConditionCheck': {'TableName': self.progress_store.registry_table,
                    'Key': encode({'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
                    'ConditionExpression': '#status = :active', 'ExpressionAttributeNames': {'#status': 'status'},
                    'ExpressionAttributeValues': encode({':active': 'DELETING'})}},
                {'Delete': {'TableName': self.source_table.name, 'Key': encode({'pk': pk, 'sk': 'META'}),
                    'ConditionExpression': 'attribute_not_exists(pk) OR (meal_batch_id = :id AND #v = :version)',
                    'ExpressionAttributeNames': {'#v': 'version'},
                    'ExpressionAttributeValues': encode({':id': target.meal_id, ':version': snapshot['version']})}},
            ])
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                    reason.get('Code') == 'ConditionalCheckFailed' for reason in (exc.response.get('CancellationReasons') or [])):
                raise ProfileDeletionConflict from exc
            raise
        return {'index': index, 'state': 'VERIFYING'}
