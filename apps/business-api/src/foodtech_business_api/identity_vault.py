"""Internal identity-to-Capsule linkage and guarded deletion snapshot.

No public API, authentication, scoped IAM role, at-rest key policy, Vault
writer policy or durable provisioning recovery is provided by this component.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID, uuid4

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from .capsule_writer import CapsuleWriter
from .guarded_deletion import VaultSnapshot


class IdentityVaultConflict(Exception):
    pass


@dataclass(frozen=True)
class PreparedIdentity:
    subject: str = field(repr=False)
    request_id: str
    expected_epoch: int
    vault_id: str = field(repr=False)
    internal_id: str = field(repr=False)
    locator: str = field(repr=False)
    capsule_action: dict = field(repr=False)


class IdentityVault:
    def __init__(self, *, auth_table: str, vault_table: str, capsule_table: str,
                 approved_purposes: frozenset[str], client=None):
        tables = (auth_table, vault_table, capsule_table)
        if any(not isinstance(table, str) or not table for table in tables) or len(set(tables)) != 3:
            raise ValueError("separate identity tables are required")
        self.auth_table, self.vault_table, self.capsule_table = tables
        self.client = client or boto3.client("dynamodb", region_name="ap-southeast-2")
        self.capsule = CapsuleWriter(capsule_table, approved_purposes, client=self.client)
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()

    def _item(self, values: dict) -> dict:
        return {name: self.serializer.serialize(value) for name, value in values.items()}

    @staticmethod
    def _identity(subject: str, request_id: str, epoch: int) -> None:
        try:
            if str(UUID(subject)) != subject or str(UUID(request_id)) != request_id or \
                    type(epoch) is not int or not 0 <= epoch < 2**63 - 1:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid identity provisioning request") from None

    def prepare(self, *, authenticated_subject: str, request_id: str, expected_epoch: int) -> PreparedIdentity:
        """Generate random internal identifiers; nothing is persisted yet."""
        self._identity(authenticated_subject, request_id, expected_epoch)
        locator, action = self.capsule.create_action()
        return PreparedIdentity(authenticated_subject, request_id, expected_epoch, str(uuid4()), str(uuid4()),
                                locator, action)

    def commit(self, *, authenticated_subject: str, prepared: PreparedIdentity) -> None:
        """Link auth, random Vault record and Capsule in a single transaction."""
        if not isinstance(prepared, PreparedIdentity) or prepared.subject != authenticated_subject:
            raise ValueError("identity owner mismatch")
        self._identity(authenticated_subject, prepared.request_id, prepared.expected_epoch)
        try:
            if str(UUID(prepared.vault_id)) != prepared.vault_id or str(UUID(prepared.internal_id)) != prepared.internal_id or \
                    prepared.capsule_action != self.capsule.creation_action(prepared.locator):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid prepared identity") from None
        vault = {"pk": prepared.vault_id, "sk": "META", "internal_user_id": prepared.internal_id,
                 "capsule_locator": prepared.locator, "state": "ACTIVE", "revision": 1}
        self.client.transact_write_items(ClientRequestToken=prepared.request_id, TransactItems=[
            {"Update": {"TableName": self.auth_table, "Key": self._item({"pk": f"USER#{authenticated_subject}"}),
                        "UpdateExpression": "SET vault_id = :vault, #epoch = if_not_exists(#epoch, :zero)",
                        "ConditionExpression": "attribute_not_exists(vault_id) AND attribute_not_exists(deletion_lock_request) "
                                               "AND (attribute_not_exists(suspended) OR suspended = :no) "
                                               "AND ((attribute_not_exists(#epoch) AND :expected = :zero) OR #epoch = :expected)",
                        "ExpressionAttributeNames": {"#epoch": "epoch"},
                        "ExpressionAttributeValues": self._item({":vault": prepared.vault_id, ":zero": 0,
                                                                    ":no": False, ":expected": prepared.expected_epoch})}},
            {"Put": {"TableName": self.vault_table, "Item": self._item(vault),
                     "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
            prepared.capsule_action,
        ])

    def resolve_deletion(self, *, authenticated_subject: str, expected_epoch: int) -> tuple[VaultSnapshot, bytearray]:
        """Resolve only one authenticated owner; reject changes or inaccessible records.

        The caller must verify the session, use a role scoped to that owner, and
        ensure every identity/Capsule writer respects locks and revisions.
        """
        self._identity(authenticated_subject, str(UUID(int=0)), expected_epoch)
        auth_key = self._item({"pk": f"USER#{authenticated_subject}"})
        def get(table, key):
            raw = self.client.get_item(TableName=table, Key=self._item(key), ConsistentRead=True).get("Item")
            return {name: self.deserializer.deserialize(value) for name, value in raw.items()} if raw else None
        def valid_principal(value):
            return value and ("suspended" not in value or value["suspended"] is False) and \
                "deletion_lock_request" not in value and \
                isinstance(value.get("epoch"), Decimal) and value["epoch"].is_finite() and \
                value["epoch"] == expected_epoch and isinstance(value.get("vault_id"), str)
        try:
            first_raw = self.client.get_item(TableName=self.auth_table, Key=auth_key, ConsistentRead=True).get("Item")
            first = {name: self.deserializer.deserialize(value) for name, value in first_raw.items()} if first_raw else None
            if not valid_principal(first) or str(UUID(first["vault_id"])) != first["vault_id"]:
                raise ValueError
            vault = get(self.vault_table, {"pk": first["vault_id"], "sk": "META"})
            if not vault or vault.get("pk") != first["vault_id"] or vault.get("sk") != "META" or \
                    vault.get("state") != "ACTIVE" or "deletion_request_id" in vault or \
                    str(UUID(vault.get("internal_user_id"))) != vault.get("internal_user_id"):
                raise ValueError
            locator = vault.get("capsule_locator")
            self.capsule._locator(locator)
            capsule = get(self.capsule_table, {"pk": locator, "sk": "META"})
            if not capsule or capsule.get("pk") != locator or capsule.get("sk") != "META" or \
                    capsule.get("state") != "ACTIVE" or "deletion_request_id" in capsule:
                raise ValueError
            for value in (vault.get("revision"), capsule.get("revision")):
                if not isinstance(value, Decimal) or not value.is_finite() or not 1 <= value < 2**63 - 1 or value != int(value):
                    raise ValueError
            last_raw = self.client.get_item(TableName=self.auth_table, Key=auth_key, ConsistentRead=True).get("Item")
            last = {name: self.deserializer.deserialize(value) for name, value in last_raw.items()} if last_raw else None
            if not valid_principal(last) or last["vault_id"] != first["vault_id"]:
                raise ValueError
            return (VaultSnapshot(authenticated_subject, first["vault_id"], int(vault["revision"]), locator,
                                  int(capsule["revision"]), expected_epoch), bytearray(vault["internal_user_id"].encode("utf-8")))
        except (TypeError, ValueError, AttributeError, KeyError, ArithmeticError):
            raise IdentityVaultConflict("identity linkage is unavailable or changed") from None
