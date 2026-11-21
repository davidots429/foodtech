"""Restricted, encrypted deletion-worker progress; not a completion receipt.

Only an authenticated maintenance worker may call this adapter. Callers must
bind encrypted checkpoints to their request, domain and version before use.
"""
import re
from uuid import UUID

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError


class DeletionProgressConflict(Exception):
    pass


class DeletionProgressStore:
    def __init__(self, *, auth_table: str, registry_table: str, progress_table: str, client=None):
        names = (auth_table, registry_table, progress_table)
        if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != 3:
            raise ValueError('separate deletion storage tables required')
        self.auth_table, self.registry_table, self.progress_table = names
        self.client = client or boto3.client('dynamodb', region_name='ap-southeast-2')
        self.serializer = TypeSerializer()

    def _encode(self, values):
        return {key: self.serializer.serialize(value) for key, value in values.items()}

    def save_checkpoint(self, *, cipher, subject: str, request_id: str, domain: str,
                        expected_version: int, state: str, checkpoint: bytes) -> int:
        """Seal a checkpoint for its next version before the guarded write."""
        if type(expected_version) is not int or not 0 <= expected_version < 2**63 - 1:
            raise ValueError('invalid deletion progress')
        ciphertext = cipher.seal(subject=subject, request_id=request_id, domain=domain,
                                 version=expected_version + 1, state=state, checkpoint=checkpoint)
        return self.save(subject=subject, request_id=request_id, domain=domain, expected_version=expected_version,
                         state=state, encrypted_checkpoint=ciphertext)

    def save(self, *, subject: str, request_id: str, domain: str, expected_version: int,
             state: str, encrypted_checkpoint: bytes) -> int:
        """Advance one domain atomically under the matching active deletion lock.

        This method never declares a domain or the whole request complete. A
        checkpoint must be authenticated encryption produced by the worker;
        this adapter cannot verify its content or binding.
        """
        try:
            if str(UUID(subject)) != subject or str(UUID(request_id)) != request_id or \
                    not isinstance(domain, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', domain) or \
                    type(expected_version) is not int or not 0 <= expected_version < 2**63 - 1 or \
                    state not in {'IN_PROGRESS', 'VERIFYING', 'FAILED'} or \
                    not isinstance(encrypted_checkpoint, bytes) or not 1 <= len(encrypted_checkpoint) <= 65536:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('invalid deletion progress') from None
        encode = self._encode
        condition = 'attribute_not_exists(pk)' if expected_version == 0 else '#version = :expected'
        values = {':next': expected_version + 1, ':state': state, ':checkpoint': encrypted_checkpoint}
        if expected_version:
            values[':expected'] = expected_version
        try:
            self.client.transact_write_items(TransactItems=[
                {'ConditionCheck': {'TableName': self.auth_table, 'Key': encode({'pk': f'USER#{subject}'}),
                    'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
                    'ExpressionAttributeValues': encode({':request': request_id, ':yes': True})}},
                {'ConditionCheck': {'TableName': self.registry_table,
                    'Key': encode({'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
                    'ConditionExpression': '#status = :active', 'ExpressionAttributeNames': {'#status': 'status'},
                    'ExpressionAttributeValues': encode({':active': 'DELETING'})}},
                {'Update': {'TableName': self.progress_table,
                    'Key': encode({'pk': f'REQ#{request_id}', 'sk': f'DOMAIN#{domain}'}),
                    'UpdateExpression': 'SET #version = :next, #state = :state, encrypted_checkpoint = :checkpoint',
                    'ConditionExpression': condition, 'ExpressionAttributeNames': {'#version': 'version', '#state': 'state'},
                    'ExpressionAttributeValues': encode(values)}}])
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                    reason.get('Code') == 'ConditionalCheckFailed'
                    for reason in (exc.response.get('CancellationReasons') or [])):
                raise DeletionProgressConflict from exc
            raise
        return expected_version + 1
