"""Remove only the expired, locked Cognito subject; never erase by email.

Internal dedicated-role boundary. The caller must authorize the deletion job.
This releases the old identity while retaining the principal lock for remaining
data erasure and fenced retirement. It never reports complete account erasure.
"""
import re
import time
from dataclasses import dataclass, field
from uuid import UUID
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import Binary
from botocore.exceptions import BotoCoreError, ClientError

from .account_withdrawal import GRACE_SECONDS, WithdrawalConflict, _integer
from .subject_identity import valid_subject


class WithdrawalIdentityConflict(Exception):
    pass


@dataclass(frozen=True)
class IdentityReleaseCursor:
    # Auth scan keys may contain session tokens. Trusted worker memory only:
    # never log, serialize, persist or expose this cursor through HTTP.
    table_name: str = field(repr=False)
    key: dict = field(repr=False)


class CognitoWithdrawalRelease:
    def __init__(self, *, auth_table, registry_table, enrollment, cognito_client, pool_id, clock=time.time):
        if not registry_table or registry_table == auth_table.name or cognito_client is None or \
                enrollment.table.name != auth_table.name or not isinstance(pool_id, str) or \
                not re.fullmatch(r'ap-southeast-2_[A-Za-z0-9]+', pool_id):
            raise ValueError('explicit separated withdrawal identity boundaries are required')
        self.auth_table, self.registry_table, self.enrollment = auth_table, registry_table, enrollment
        self.cognito, self.pool_id, self.clock = cognito_client, pool_id, clock

    def _authorization(self, subject, request_id, expected_epoch):
        response = self.auth_table.meta.client.transact_get_items(TransactItems=[
            {'Get': {'TableName': self.auth_table.name, 'Key': {'pk': f'USER#{subject}'}}},
            {'Get': {'TableName': self.registry_table, 'Key': {'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}}},
        ])
        try:
            rows = response['Responses']
            if not isinstance(rows, list) or len(rows) != 2:
                raise ValueError
            account, registry = (row['Item'] for row in rows)
            if account.get('pk') != f'USER#{subject}' or account.get('suspended') is not True or \
                    account.get('deletion_lock_request') != request_id or account.get('withdrawal_request_id') != request_id or \
                    account.get('withdrawal_status') != 'DELETING' or _integer(account.get('epoch'), 1) != expected_epoch or \
                    registry.get('pk') != 'REGISTRY' or registry.get('sk') != f'REQ#{request_id}' or \
                    registry.get('request_id') != request_id or registry.get('status') != 'DELETING' or \
                    ('identity_retired_request' in account and account['identity_retired_request'] != request_id):
                raise ValueError
            _integer(registry.get('version'), 1)
            if _integer(account.get('withdrawal_epoch'), 1) >= expected_epoch or any(
                        not isinstance(registry.get(field), (bytes, bytearray, Binary)) or
                        not 1 <= len(bytes(registry[field])) <= 65536
                        for field in ('encrypted_user_id', 'encrypted_capsule_locator')):
                raise ValueError
            requested = _integer(account.get('withdrawal_requested_at'), 1)
            deadline = _integer(account.get('withdrawal_deadline'), 1)
            if deadline - requested != GRACE_SECONDS or int(self.clock()) < deadline:
                raise ValueError
            return requested, deadline
        except (KeyError, ValueError, TypeError, AttributeError, WithdrawalConflict):
            raise WithdrawalIdentityConflict('expired locked withdrawal is unavailable') from None

    def _get_user(self, subject):
        try:
            user = self.cognito.admin_get_user(UserPoolId=self.pool_id, Username=subject)
        except self.cognito.exceptions.UserNotFoundException:
            return None
        attributes = user.get('UserAttributes', [])
        if not isinstance(attributes, list):
            raise WithdrawalIdentityConflict('provider identity attributes are unavailable')
        subjects = [item.get('Value') for item in attributes if isinstance(item, dict) and item.get('Name') == 'sub']
        if subjects != [subject]:
            raise WithdrawalIdentityConflict('provider identity does not match the locked subject')
        return user

    def release(self, *, subject, request_id, expected_epoch):
        try:
            if not valid_subject(subject) or str(UUID(request_id)) != request_id:
                raise ValueError
            expected_epoch = _integer(expected_epoch, 1)
        except (ValueError, TypeError, AttributeError, WithdrawalConflict):
            raise ValueError('invalid withdrawal identity request') from None
        self.enrollment.fence()
        requested, deadline = self._authorization(subject, request_id, expected_epoch)
        if self._get_user(subject) is not None:
            # Never substitute a reusable Username/email returned by Cognito.
            # Recheck the durable authorization just before irreversible I/O.
            if self._authorization(subject, request_id, expected_epoch) != (requested, deadline):
                raise WithdrawalIdentityConflict('withdrawal schedule changed')
            try:
                self.cognito.admin_delete_user(UserPoolId=self.pool_id, Username=subject)
            except self.cognito.exceptions.UserNotFoundException:
                pass
        if self._get_user(subject) is not None:
            raise WithdrawalIdentityConflict('provider identity absence is not confirmed')
        # The account guard remains. An uncertain write must be reconciled or
        # retried using fresh identity/state reads; do not recreate old accounts.
        self.auth_table.meta.client.transact_write_items(TransactItems=[
            {'Update': {'TableName': self.auth_table.name, 'Key': {'pk': f'USER#{subject}'},
                'UpdateExpression': 'SET identity_retired_request = :request, identity_retired_at = :now',
                'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes AND #epoch = :epoch '
                    'AND withdrawal_request_id = :request AND #withdrawal = :deleting '
                    'AND withdrawal_requested_at = :requested AND withdrawal_deadline = :deadline '
                    'AND (attribute_not_exists(identity_retired_request) OR identity_retired_request = :request)',
                'ExpressionAttributeNames': {'#epoch': 'epoch', '#withdrawal': 'withdrawal_status'},
                'ExpressionAttributeValues': {':request': request_id, ':yes': True, ':epoch': expected_epoch,
                    ':deleting': 'DELETING', ':requested': requested, ':deadline': deadline, ':now': int(self.clock())}}},
            {'ConditionCheck': {'TableName': self.registry_table,
                'Key': {'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'},
                'ConditionExpression': '#status = :deleting AND request_id = :request',
                'ExpressionAttributeNames': {'#status': 'status'},
                'ExpressionAttributeValues': {':deleting': 'DELETING', ':request': request_id}}},
        ])
        return {'state': 'COGNITO_REMOVED', 'deletion_complete': False}

    def scan_page(self, *, limit=20, cursor=None):
        """One bounded worker pass over locked identities, without data erasure.

        A separate narrowly authorized identity worker runs this stage; it does
        not need Vault/Capsule plaintext or permission to mark Registry complete.
        Each candidate is reauthorized transactionally by release(), so scan
        observations alone never authorize Cognito deletion. Restart from the
        beginning after process loss; per-account markers make retries safe.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid identity release page size')
        args = {'Limit': limit, 'ConsistentRead': True, 'Select': 'SPECIFIC_ATTRIBUTES',
            'ProjectionExpression': 'pk, withdrawal_request_id, #epoch',
            'ExpressionAttributeNames': {'#epoch': 'epoch'},
            'FilterExpression': Attr('pk').begins_with('USER#') & Attr('withdrawal_status').eq('DELETING') &
                Attr('suspended').eq(True) & Attr('withdrawal_deadline').lte(int(self.clock())) &
                Attr('deletion_lock_request').exists() & Attr('identity_retired_request').not_exists()}
        if cursor is not None:
            if not isinstance(cursor, IdentityReleaseCursor) or cursor.table_name != self.auth_table.name or \
                    not isinstance(cursor.key, dict) or set(cursor.key) != {'pk'} or \
                    not isinstance(cursor.key['pk'], str) or not 1 <= len(cursor.key['pk']) <= 2048:
                raise ValueError('invalid identity release continuation')
            args['ExclusiveStartKey'] = dict(cursor.key)
        # Discovery failure propagates without manufacturing an end cursor.
        page = self.auth_table.scan(**args)
        results = []
        for item in page.get('Items', []):
            try:
                if not isinstance(item, dict) or not isinstance(item.get('pk'), str) or not item['pk'].startswith('USER#'):
                    raise ValueError('invalid identity release candidate')
                results.append(self.release(subject=item['pk'].removeprefix('USER#'),
                    request_id=item['withdrawal_request_id'], expected_epoch=item['epoch']))
            except (KeyError, ValueError, WithdrawalConflict, WithdrawalIdentityConflict):
                results.append({'state': 'BLOCKED', 'deletion_complete': False})
            except (BotoCoreError, ClientError):
                # Delete or marker update may have committed. Re-read and retry
                # the exact old subject; never assume completion after timeout.
                results.append({'state': 'RECONCILIATION_REQUIRED', 'deletion_complete': False})
        key = page.get('LastEvaluatedKey')
        return {'results': results, 'next_cursor': IdentityReleaseCursor(self.auth_table.name, dict(key)) if key else None}
