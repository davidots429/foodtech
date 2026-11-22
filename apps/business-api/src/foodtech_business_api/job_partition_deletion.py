"""Bounded deletion of one subject-owned job partition, retaining its META.

Internal primitive only: an authenticated worker must stop all writers, capture
job IDs without omissions, clean reverse bindings and other stores, then verify
residuals. META_ONLY is an observation, never an account-deletion receipt.
"""
import re
from uuid import UUID

from .subject_identity import valid_subject

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .condition_profile_deletion import ProfileDeletionConflict


def delete_one_job_record(*, source_table, progress_store, cipher, subject, request_id, job_id):
    try:
        if not valid_subject(subject) or str(UUID(request_id)) != request_id or \
                not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', job_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid job deletion scope') from None
    if source_table.table_arn != getattr(cipher, 'source_table_arn', None) or source_table.name in {
            progress_store.auth_table, progress_store.registry_table, progress_store.progress_table}:
        raise ValueError('job source does not match deletion scope')
    progress_store.load_checkpoint(cipher=cipher, subject=subject, request_id=request_id,
                                   domain='JOB_PARTITION')
    pk = f'SCOPE#USER#{subject}#JOB#{job_id}'
    page = source_table.query(KeyConditionExpression=Key('pk').eq(pk), ConsistentRead=True, Limit=2)
    rows = page.get('Items')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError('job partition unavailable')
    keys = []
    for row in rows:
        if not isinstance(row, dict) or row.get('pk') != pk or not isinstance(row.get('sk'), str) or \
                not 1 <= len(row['sk']) <= 1024 or row['sk'] in keys:
            raise ValueError('invalid job partition page')
        keys.append(row['sk'])
    candidates = [sk for sk in keys if sk != 'META']
    if not candidates:
        meta = rows[0]
        if page.get('LastEvaluatedKey') is not None or meta.get('owner_subject') != subject or \
                meta.get('job_id') != job_id:
            raise ValueError('job partition unavailable')
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
                'ConditionExpression': 'owner_subject = :owner AND job_id = :id',
                'ExpressionAttributeValues': encode({':owner': subject, ':id': job_id})}},
            {'Delete': {'TableName': source_table.name, 'Key': encode({'pk': pk, 'sk': candidates[0]}),
                'ConditionExpression': 'attribute_exists(pk)'}},
        ])
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                reason.get('Code') == 'ConditionalCheckFailed' for reason in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
    return {'state': 'IN_PROGRESS', 'removed': True}
