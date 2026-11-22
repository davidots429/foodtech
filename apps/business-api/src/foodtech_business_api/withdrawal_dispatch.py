"""Internal bounded discovery and expiry-to-lock dispatch, without erasure.

Use a separately authorized worker identity, never a customer or web API role.
Successful locking does not prove writer quiescence or deletion completion.
Restarting a scan is safe; account transitions and locks are conditional.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

from .account_withdrawal import DynamoWithdrawalStore, WithdrawalConflict, _integer, _identity
from .identity_vault import IdentityVaultConflict


@dataclass(frozen=True)
class WithdrawalScanCursor:
    # Auth scan continuation keys can refer to a session item. Keep them in
    # trusted memory only; never log, serialize, return through HTTP, or persist.
    table_name: str = field(repr=False)
    key: dict = field(repr=False)


class WithdrawalDispatcher:
    def __init__(self, *, auth_table, identity, cipher, deletion, clock=time.time):
        if auth_table.name != identity.auth_table or identity.auth_table != deletion.auth_table or \
                identity.vault_table != deletion.vault_table or identity.capsule_table != deletion.capsule_table:
            raise ValueError('withdrawal dispatch security domains do not match')
        self.auth_table, self.identity, self.cipher, self.deletion = auth_table, identity, cipher, deletion
        self.withdrawal = DynamoWithdrawalStore(table=auth_table)
        self.clock = clock

    def step(self, *, subject, request_id):
        now = int(self.clock())
        _identity(subject, request_id, now)
        record = self.auth_table.get_item(Key={'pk': f'USER#{subject}'}, ConsistentRead=True).get('Item')
        if not isinstance(record, dict) or record.get('withdrawal_request_id') != request_id or \
                not isinstance(record.get('withdrawal_status'), str) or \
                record['withdrawal_status'] not in {'PENDING', 'DELETING'}:
            return {'state': 'STALE'}
        if 'deletion_lock_request' in record:
            # A previous response might have been lost. An independent guarded
            # recovery boundary must verify all locks and ciphertext ownership.
            return {'state': 'RECOVERY_REQUIRED'}
        if _integer(record.get('withdrawal_deadline'), 1) > now:
            return {'state': 'NOT_DUE'}
        expired = self.withdrawal.transition(subject=subject, request_id=request_id, now=now,
            action='expire', expected_epoch=_integer(record.get('epoch')))
        snapshot, internal_id = self.identity.resolve_deletion(authenticated_subject=subject,
            expected_epoch=_integer(expired['epoch']), withdrawal_request_id=request_id)
        locator = bytearray(snapshot.capsule_locator.encode('ascii'))
        try:
            encrypted = self.cipher.seal(request_id, internal_id, locator)
            self.deletion.lock_and_register(subject=subject, request_id=request_id, snapshot=snapshot,
                encrypted_user_id=encrypted['internal_user_id'], encrypted_capsule_locator=encrypted['capsule_locator'],
                requested_at=datetime.fromtimestamp(snapshot.withdrawal_requested_at, timezone.utc).isoformat(),
                policy_version='withdrawal-7d-v1', attempt_id=str(uuid4()))
        finally:
            for buffer in (internal_id, locator):
                buffer[:] = b'\x00' * len(buffer)
        return {'state': 'LOCKED'}

    def scan_page(self, *, limit=20, cursor=None):
        """Process at most one bounded Scan page; empty pages can have a cursor.

        The cursor is transient sensitive worker memory. Its absence only ends
        this scan observation; it is never evidence of completed deletion.
        Storage/service failures propagate without advancing the cursor so the
        caller can retry or restart. Never treat an interrupted scan as done.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid withdrawal dispatch page size')
        args = {'Limit': limit, 'ConsistentRead': True,
            'ProjectionExpression': 'pk, withdrawal_request_id',
            'FilterExpression': Attr('pk').begins_with('USER#') &
                Attr('withdrawal_status').is_in(['PENDING', 'DELETING']) &
                Attr('withdrawal_deadline').lte(int(self.clock())) & Attr('deletion_lock_request').not_exists()}
        if cursor is not None:
            if not isinstance(cursor, WithdrawalScanCursor) or cursor.table_name != self.auth_table.name or \
                    not isinstance(cursor.key, dict) or set(cursor.key) != {'pk'} or \
                    not isinstance(cursor.key['pk'], str) or not 1 <= len(cursor.key['pk']) <= 2048:
                raise ValueError('invalid withdrawal scan continuation')
            args['ExclusiveStartKey'] = dict(cursor.key)
        page = self.auth_table.scan(**args)
        results = []
        for item in page.get('Items', []):
            try:
                if not isinstance(item, dict):
                    raise ValueError('invalid withdrawal candidate')
                pk, request_id = item['pk'], item['withdrawal_request_id']
                if not isinstance(pk, str) or not pk.startswith('USER#'):
                    raise ValueError('invalid withdrawal candidate')
                results.append(self.step(subject=pk.removeprefix('USER#'), request_id=request_id))
            except (KeyError, ValueError, WithdrawalConflict, IdentityVaultConflict):
                results.append({'state': 'BLOCKED'})
            except (BotoCoreError, ClientError):
                # May have committed. No candidate or completion receipt is
                # emitted; a later pass or guarded recovery must inspect state.
                results.append({'state': 'RECONCILIATION_REQUIRED'})
        key = page.get('LastEvaluatedKey')
        return {'results': results, 'next_cursor': WithdrawalScanCursor(self.auth_table.name, dict(key)) if key else None}
