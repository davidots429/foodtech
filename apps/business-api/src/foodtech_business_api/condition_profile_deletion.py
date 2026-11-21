"""Locked atomic deletion of one known condition-profile version and reference.

Only a separately authorized worker with a complete, protected inventory may
call this primitive. It does not discover legacy rows, delete jobs or establish
customer deletion completion.
"""
import re
from uuid import UUID

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .cooking_store import _profile_listing


class ProfileDeletionConflict(Exception):
    pass


def delete_profile_pair(table, client, *, auth_table: str, registry_table: str, subject: str, request_id: str,
                        profile_id: str, version: str) -> None:
    try:
        if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', subject) or \
                str(UUID(request_id)) != request_id or not isinstance(auth_table, str) or not auth_table or \
                not isinstance(registry_table, str) or not registry_table or \
                not isinstance(getattr(table, 'name', None), str) or not table.name or \
                len({table.name, auth_table, registry_table}) != 3:
            raise ValueError
        listing = _profile_listing(subject, {'profile_id': profile_id, 'profile_version': version})
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('invalid profile deletion scope') from None
    serializer = TypeSerializer()
    def encode(values):
        return {key: serializer.serialize(value) for key, value in values.items()}
    values = encode({':id': profile_id, ':version': version})
    condition = 'attribute_not_exists(pk) OR (profile_id = :id AND profile_version = :version)'
    source = {'pk': f'SCOPE#USER#{subject}#CONDITION_PROFILE#{profile_id}', 'sk': f'VERSION#{version}'}
    reference = {'pk': listing['pk'], 'sk': listing['sk']}
    try:
        client.transact_write_items(TransactItems=[
            {'ConditionCheck': {'TableName': auth_table, 'Key': encode({'pk': f'USER#{subject}'}),
                                'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
                                'ExpressionAttributeValues': encode({':request': request_id, ':yes': True})}},
            {'ConditionCheck': {'TableName': registry_table,
                                'Key': encode({'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
                                'ConditionExpression': '#status = :active',
                                'ExpressionAttributeNames': {'#status': 'status'},
                                'ExpressionAttributeValues': encode({':active': 'DELETING'})}},
            *[{'Delete': {'TableName': table.name, 'Key': encode(key), 'ConditionExpression': condition,
                          'ExpressionAttributeValues': values}} for key in (source, reference)],
        ])
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                reason.get('Code') == 'ConditionalCheckFailed'
                for reason in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
