"""Bounded cleanup for one captured meal; META and listing are retained.

An authorized deletion worker must first capture all targets and stop writers.
META_ONLY is an observation, not proof of deletion or ownership of source data.
"""
import re
from uuid import UUID

from .subject_identity import valid_subject

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .condition_profile_deletion import ProfileDeletionConflict


def delete_one_meal_record(*, source_table, progress_store, cipher, subject, request_id, meal_id):
    try:
        if not valid_subject(subject) or str(UUID(request_id)) != request_id or not isinstance(meal_id, str) or \
                not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', meal_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid meal deletion scope') from None
    if source_table.table_arn != getattr(cipher, 'source_table_arn', None) or source_table.name in {
            progress_store.auth_table, progress_store.registry_table, progress_store.progress_table}:
        raise ValueError('meal source does not match deletion scope')
    progress_store.load_checkpoint(cipher=cipher, subject=subject, request_id=request_id, domain='MEAL_PARTITION')
    pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{meal_id}'
    page = source_table.query(KeyConditionExpression=Key('pk').eq(pk), ConsistentRead=True, Limit=2)
    rows = page.get('Items')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError('meal partition unavailable')
    keys = []
    for row in rows:
        if not isinstance(row, dict) or row.get('pk') != pk or not isinstance(row.get('sk'), str) or \
                not 1 <= len(row['sk']) <= 1024 or row['sk'] in keys:
            raise ValueError('invalid meal partition page')
        keys.append(row['sk'])
    candidates = [sk for sk in keys if sk != 'META']
    if not candidates:
        if page.get('LastEvaluatedKey') is not None or rows[0].get('meal_batch_id') != meal_id:
            raise ValueError('meal partition unavailable')
        return {'state': 'META_ONLY', 'removed': False}
    serializer = TypeSerializer()

    def encode(values):
        return {key: serializer.serialize(value) for key, value in values.items()}

    try:
        progress_store.client.transact_write_items(TransactItems=[
            {'ConditionCheck': {'TableName': progress_store.auth_table, 'Key': encode({'pk': f'USER#{subject}'}),
                'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
                'ExpressionAttributeValues': encode({':request': request_id, ':yes': True})}},
            {'ConditionCheck': {'TableName': progress_store.registry_table,
                'Key': encode({'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
                'ConditionExpression': '#status = :active', 'ExpressionAttributeNames': {'#status': 'status'},
                'ExpressionAttributeValues': encode({':active': 'DELETING'})}},
            {'ConditionCheck': {'TableName': source_table.name, 'Key': encode({'pk': pk, 'sk': 'META'}),
                'ConditionExpression': 'meal_batch_id = :id',
                'ExpressionAttributeValues': encode({':id': meal_id})}},
            {'Delete': {'TableName': source_table.name, 'Key': encode({'pk': pk, 'sk': candidates[0]}),
                'ConditionExpression': 'attribute_exists(pk)'}},
        ])
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                reason.get('Code') == 'ConditionalCheckFailed' for reason in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
    return {'state': 'IN_PROGRESS', 'removed': True}
