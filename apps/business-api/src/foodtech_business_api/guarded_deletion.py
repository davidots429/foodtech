"""Prepare one atomic deletion lock across separate security domains.

This is an internal primitive. It does not authenticate callers, resolve Vault
records, issue scoped roles, erase data or expose a public deletion API.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import boto3
from boto3.dynamodb.types import TypeSerializer

from .capsule_writer import CapsuleWriter


@dataclass(frozen=True)
class VaultSnapshot:
    """Trusted server-side resolution; never populate from a request body."""
    authenticated_subject: str = field(repr=False)
    vault_id: str = field(repr=False)
    vault_revision: int
    capsule_locator: str = field(repr=False)
    capsule_revision: int
    auth_epoch: int


class GuardedDeletion:
    def __init__(self, *, auth_table: str, registry_table: str, vault_table: str,
                 capsule_table: str, approved_purposes: frozenset[str], client=None):
        tables = (auth_table, registry_table, vault_table, capsule_table)
        if any(not isinstance(table, str) or not table for table in tables) or len(set(tables)) != 4:
            raise ValueError("separate security-domain tables are required")
        self.auth_table, self.registry_table, self.vault_table = auth_table, registry_table, vault_table
        self.capsule_table = capsule_table
        self.client = client or boto3.client("dynamodb", region_name="ap-southeast-2")
        self.writer = CapsuleWriter(capsule_table, approved_purposes, client=self.client)
        self.serializer = TypeSerializer()

    def _item(self, values: dict) -> dict:
        return {name: self.serializer.serialize(value) for name, value in values.items()}

    def recover_locked_request(self, *, subject: str, request_id: str, vault_id: str, internal_user_id: str,
                               capsule_locator: str) -> dict:
        """Confirm a known locked request from one consistent four-table snapshot.

        The caller must authenticate the owner and resolve identifiers through
        the trusted recovery boundary. No ciphertext or locator is returned. This
        checks the lock only; it does not prove deletion or backup reconciliation.
        """
        try:
            if str(UUID(subject)) != subject or str(UUID(request_id)) != request_id or str(UUID(vault_id)) != vault_id or \
                    str(UUID(internal_user_id)) != internal_user_id or \
                    not isinstance(capsule_locator, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', capsule_locator):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid guarded recovery scope') from None
        get = lambda table, key: {'Get': {'TableName': table, 'Key': self._item(key)}}
        result = self.client.transact_get_items(TransactItems=[
            get(self.auth_table, {'pk': f'USER#{subject}'}),
            get(self.vault_table, {'pk': vault_id, 'sk': 'META'}),
            get(self.capsule_table, {'pk': capsule_locator, 'sk': 'META'}),
            get(self.registry_table, {'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
        ])
        try:
            records = result['Responses']
            if not isinstance(records, list) or len(records) != 4:
                raise ValueError
            auth, vault, capsule, registry = (record.get('Item') for record in records)
            if not all(isinstance(item, dict) for item in (auth, vault, capsule, registry)) or \
                    auth.get('pk') != {'S': f'USER#{subject}'} or auth.get('vault_id') != {'S': vault_id} or \
                    auth.get('suspended') != {'BOOL': True} or auth.get('deletion_lock_request') != {'S': request_id} or \
                    vault.get('pk') != {'S': vault_id} or vault.get('sk') != {'S': 'META'} or \
                    vault.get('capsule_locator') != {'S': capsule_locator} or \
                    vault.get('internal_user_id') != {'S': internal_user_id} or \
                    capsule.get('pk') != {'S': capsule_locator} or capsule.get('sk') != {'S': 'META'} or \
                    any(item.get('state') != {'S': 'DELETION_LOCKED'} or
                        item.get('deletion_request_id') != {'S': request_id} for item in (vault, capsule)) or \
                    registry.get('pk') != {'S': 'REGISTRY'} or registry.get('sk') != {'S': f'REQ#{request_id}'} or \
                    registry.get('request_id') != {'S': request_id} or registry.get('status') != {'S': 'DELETING'} or \
                    any(not isinstance(registry.get(field, {}).get('B'), bytes) or not registry[field]['B']
                        for field in ('encrypted_user_id', 'encrypted_capsule_locator')):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('guarded deletion recovery unavailable') from None
        return {'status': 'DELETING'}

    def lock_and_register(self, *, subject: str, request_id: str, snapshot: VaultSnapshot,
                          encrypted_user_id: bytes, encrypted_capsule_locator: bytes,
                          requested_at: str, policy_version: str) -> None:
        """Atomically lock auth, Vault and Capsule and register encrypted tombstone.

        Ciphertexts must be made from the same trusted snapshot and request. All
        Vault and Capsule writers must honor their locks and advance revisions.
        Repeat with the identical inputs and request ID for DynamoDB's bounded
        client-token retry window. This method does not solve durable saga resume.
        """
        try:
            if not isinstance(snapshot, VaultSnapshot) or snapshot.authenticated_subject != subject or \
                    str(UUID(subject)) != subject or str(UUID(request_id)) != request_id or \
                    str(UUID(snapshot.vault_id)) != snapshot.vault_id:
                raise ValueError
            if type(snapshot.vault_revision) is not int or not 1 <= snapshot.vault_revision < 2**63 - 1:
                raise ValueError
            if type(snapshot.auth_epoch) is not int or not 0 <= snapshot.auth_epoch < 2**63 - 1:
                raise ValueError
            when = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            if when.tzinfo is None or when.utcoffset() != timezone.utc.utcoffset(when):
                raise ValueError
            if not isinstance(policy_version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", policy_version):
                raise ValueError
            if any(not isinstance(value, bytes) or not 1 <= len(value) <= 65536
                   for value in (encrypted_user_id, encrypted_capsule_locator)):
                raise ValueError
            capsule_action = self.writer.lock_action(locator=snapshot.capsule_locator, request_id=request_id,
                                                     expected_revision=snapshot.capsule_revision)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid guarded deletion request") from None
        tombstone = {"pk": "REGISTRY", "sk": f"REQ#{request_id}", "request_id": request_id,
                     "requested_at": requested_at, "completed_at": None, "status": "DELETING",
                     "encrypted_user_id": encrypted_user_id, "encrypted_capsule_locator": encrypted_capsule_locator,
                     "reconcile_until": None, "expires_at": None, "ttl_delete_after": None,
                     "policy_version": policy_version, "version": 1}
        self.client.transact_write_items(ClientRequestToken=request_id, TransactItems=[
            {"Update": {"TableName": self.auth_table, "Key": self._item({"pk": f"USER#{subject}"}),
                        "UpdateExpression": "SET suspended = :yes, deletion_lock_request = :request ADD #epoch :one",
                        "ConditionExpression": "attribute_not_exists(deletion_lock_request) AND vault_id = :vault "
                                               "AND #epoch = :expected_epoch AND (attribute_not_exists(suspended) OR suspended = :no)",
                        "ExpressionAttributeNames": {"#epoch": "epoch"},
                        "ExpressionAttributeValues": self._item({":yes": True, ":no": False, ":request": request_id, ":one": 1,
                                                                   ":vault": snapshot.vault_id,
                                                                   ":expected_epoch": snapshot.auth_epoch})}},
            {"Update": {"TableName": self.vault_table, "Key": self._item({"pk": snapshot.vault_id, "sk": "META"}),
                        "UpdateExpression": "SET #state = :locked, #revision = :next, deletion_request_id = :request",
                        "ConditionExpression": "#state = :active AND #revision = :expected AND capsule_locator = :locator "
                                               "AND attribute_not_exists(deletion_request_id)",
                        "ExpressionAttributeNames": {"#state": "state", "#revision": "revision"},
                        "ExpressionAttributeValues": self._item({":active": "ACTIVE", ":locked": "DELETION_LOCKED",
                            ":expected": snapshot.vault_revision, ":next": snapshot.vault_revision + 1,
                            ":locator": snapshot.capsule_locator, ":request": request_id})}},
            capsule_action,
            {"Put": {"TableName": self.registry_table, "Item": self._item(tombstone),
                     "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
        ])
