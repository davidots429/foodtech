"""Bounded removal of orphan condition-profile listing records.

Use only from an authenticated deletion worker after legacy writers are stopped
and captured source profiles have been handled. An empty query is an observation,
not a completion receipt or a substitute for a subsequent source-table scan.
"""
import re
from uuid import UUID

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .condition_profile_deletion import ProfileDeletionConflict
from .cooking_store import _profile_listing


def clean_one_orphan(*, source_table, progress_store, cipher, subject, request_id):
    if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', subject):
        raise ValueError('invalid profile cleanup scope')
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid profile cleanup scope') from None
    if source_table.table_arn != getattr(cipher, 'source_table_arn', None) or source_table.name in {
            progress_store.auth_table, progress_store.progress_table}:
        raise ValueError('profile source does not match cleanup scope')
    # The protected load verifies the suspended account, request lock and registry.
    progress_store.load_checkpoint(cipher=cipher, subject=subject, request_id=request_id,
                                   domain='PROFILE_INVENTORY')
    owner = f'SCOPE#USER#{subject}#CONDITION_PROFILES'
    result = source_table.query(KeyConditionExpression=Key('pk').eq(owner), ConsistentRead=True, Limit=1)
    items = result.get('Items')
    if not isinstance(items, list) or len(items) > 1:
        raise ValueError('invalid profile listing page')
    if not items:
        if result.get('LastEvaluatedKey') is not None:
            raise ValueError('incomplete profile listing page')
        return {'state': 'VERIFYING', 'removed': False}
    item = items[0]
    try:
        profile_id, version = item['profile_id'], item['profile_version']
        expected = _profile_listing(subject, {'profile_id': profile_id, 'profile_version': version})
        if item.get('pk') != expected['pk'] or item.get('sk') != expected['sk']:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('invalid profile listing') from None
    serializer = TypeSerializer()
    def encode(values):
        return {key: serializer.serialize(value) for key, value in values.items()}
    source = {'pk': f'SCOPE#USER#{subject}#CONDITION_PROFILE#{profile_id}', 'sk': f'VERSION#{version}'}
    try:
        progress_store.client.transact_write_items(TransactItems=[
            {'ConditionCheck': {'TableName': progress_store.auth_table, 'Key': encode({'pk': f'USER#{subject}'}),
                'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
                'ExpressionAttributeValues': encode({':request': request_id, ':yes': True})}},
            {'ConditionCheck': {'TableName': source_table.name, 'Key': encode(source),
                'ConditionExpression': 'attribute_not_exists(pk)'}},
            {'Delete': {'TableName': source_table.name, 'Key': encode({'pk': expected['pk'], 'sk': expected['sk']}),
                'ConditionExpression': 'attribute_not_exists(pk) OR (profile_id = :id AND profile_version = :version)',
                'ExpressionAttributeValues': encode({':id': profile_id, ':version': version})}},
        ])
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                reason.get('Code') == 'ConditionalCheckFailed' for reason in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
    return {'state': 'VERIFYING', 'removed': True}
