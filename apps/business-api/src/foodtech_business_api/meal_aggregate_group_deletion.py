"""Remove one orphaned meal aggregate group index under a deletion lock.

The caller must stop old writers before observing the result partition.
An empty index is not proof of account deletion or backup cleanup.
"""
import re
from uuid import UUID

from .subject_identity import valid_subject

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .condition_profile_deletion import ProfileDeletionConflict


def clean_one_meal_aggregate_group(*, source_table, progress_store, cipher, subject, request_id):
    try:
        if not valid_subject(subject) or str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid meal aggregate group scope') from None
    if source_table.table_arn != getattr(cipher, 'source_table_arn', None) or source_table.name in {
            progress_store.auth_table, progress_store.registry_table, progress_store.progress_table}:
        raise ValueError('meal aggregate source does not match deletion scope')
    progress_store.load_checkpoint(cipher=cipher, subject=subject, request_id=request_id, domain='MEAL_AGGREGATE_GROUPS')
    index_pk = f'SCOPE#USER#{subject}#MEAL_AGGREGATE_GROUPS'
    page = source_table.query(KeyConditionExpression=Key('pk').eq(index_pk), ConsistentRead=True, Limit=1)
    rows = page.get('Items')
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('invalid meal aggregate group page')
    if not rows:
        if page.get('LastEvaluatedKey') is not None:
            raise ValueError('incomplete meal aggregate group page')
        return {'state': 'VERIFYING', 'removed': False}
    try:
        row = rows[0]
        group = row['group_id']
        if not isinstance(group, str) or not re.fullmatch(r'[0-9a-f]{64}', group) or \
                row['pk'] != index_pk or row['sk'] != f'GROUP#{group}':
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid meal aggregate group') from None
    group_pk = f'SCOPE#USER#{subject}#MEAL_AGGREGATE#{group}'
    residual = source_table.query(KeyConditionExpression=Key('pk').eq(group_pk), ConsistentRead=True, Limit=1)
    if residual.get('Items') != [] or residual.get('LastEvaluatedKey') is not None:
        raise ValueError('meal aggregate group is not empty')
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
            {'Delete': {'TableName': source_table.name, 'Key': encode({'pk': index_pk, 'sk': row['sk']}),
                'ConditionExpression': 'attribute_not_exists(pk) OR group_id = :group',
                'ExpressionAttributeValues': encode({':group': group})}},
        ])
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                reason.get('Code') == 'ConditionalCheckFailed' for reason in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
    return {'state': 'IN_PROGRESS', 'removed': True}
