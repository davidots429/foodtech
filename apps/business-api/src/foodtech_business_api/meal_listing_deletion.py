"""Guarded cleanup of orphaned subject-scoped meal listing references.

This is an internal primitive; callers must stop writers and verify residuals.
An empty listing does not prove whole-account deletion is complete.
"""
import re
from uuid import UUID

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .condition_profile_deletion import ProfileDeletionConflict


def clean_one_meal_listing(*, source_table, progress_store, cipher, subject, request_id):
    try:
        if str(UUID(subject)) != subject or str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid meal listing scope') from None
    if source_table.table_arn != getattr(cipher, 'source_table_arn', None) or source_table.name in {
            progress_store.auth_table, progress_store.registry_table, progress_store.progress_table}:
        raise ValueError('meal source does not match listing scope')
    progress_store.load_checkpoint(cipher=cipher, subject=subject, request_id=request_id, domain='MEAL_LISTING')
    listing_pk = f'SCOPE#USER#{subject}#COMBI_MEALS'
    page = source_table.query(KeyConditionExpression=Key('pk').eq(listing_pk) & Key('sk').begins_with('MEAL#'),
                              ConsistentRead=True, Limit=1)
    rows = page.get('Items')
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('invalid meal listing page')
    if not rows:
        if page.get('LastEvaluatedKey') is not None:
            raise ValueError('incomplete meal listing page')
        return {'removed': False, 'state': 'VERIFYING'}
    row = rows[0]
    try:
        meal_id = row['meal_batch_id']
        if not isinstance(meal_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', meal_id) or \
                row['pk'] != listing_pk or row['sk'] != f'MEAL#{meal_id}':
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid meal listing') from None
    meal_pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{meal_id}'
    partition = source_table.query(KeyConditionExpression=Key('pk').eq(meal_pk), ConsistentRead=True, Limit=1)
    if partition.get('Items') != [] or partition.get('LastEvaluatedKey') is not None:
        raise ValueError('meal partition is not empty')
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
            {'ConditionCheck': {'TableName': source_table.name, 'Key': encode({'pk': meal_pk, 'sk': 'META'}),
                'ConditionExpression': 'attribute_not_exists(pk)'}},
            {'Delete': {'TableName': source_table.name, 'Key': encode({'pk': listing_pk, 'sk': row['sk']}),
                'ConditionExpression': 'attribute_not_exists(pk) OR meal_batch_id = :id',
                'ExpressionAttributeValues': encode({':id': meal_id})}},
        ])
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                reason.get('Code') == 'ConditionalCheckFailed' for reason in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
    return {'removed': True, 'state': 'IN_PROGRESS'}
