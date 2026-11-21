"""Ciphertext-only Capsule writes with an optimistic revision and lock.

No public endpoint, identity lookup, encryption, IAM boundary or deletion
orchestration is provided by this module.
"""
import re
import secrets
from uuid import UUID

import boto3
from boto3.dynamodb.types import TypeSerializer

from .capsule_metadata import validate_key_metadata, validate_user_key_id

class CapsuleWriter:
    def __init__(self, table_name: str, allowed_purposes: frozenset[str], *, client=None):
        if not table_name or not allowed_purposes or any(
                not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value)
                for value in allowed_purposes):
            raise ValueError("table and approved purpose inventory are required")
        self.table_name, self.allowed_purposes = table_name, allowed_purposes
        self.client = client or boto3.client("dynamodb", region_name="ap-southeast-2")
        self.serializer = TypeSerializer()

    def _item(self, values: dict) -> dict:
        return {name: self.serializer.serialize(value) for name, value in values.items()}

    @staticmethod
    def _locator(locator: str) -> None:
        if not isinstance(locator, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", locator):
            raise ValueError("invalid capsule locator")

    @staticmethod
    def _revision(revision: int) -> None:
        if type(revision) is not int or not 1 <= revision < 2**63 - 1:
            raise ValueError("invalid capsule revision")

    def create_action(self) -> tuple[str, dict]:
        """Build a random Capsule put to include in the same transaction as Vault creation.

        The locator must not be returned externally until that transaction succeeds.
        """
        locator = secrets.token_urlsafe(32)
        return locator, self.creation_action(locator)

    def creation_action(self, locator: str) -> dict:
        """Build the conditional META put for a previously generated locator."""
        self._locator(locator)
        return {"Put": {"TableName": self.table_name,
            "Item": self._item({"pk": locator, "sk": "META", "state": "ACTIVE", "revision": 1}),
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}}

    def put_entry(self, *, locator: str, purpose: str, epoch: str, ciphertext: bytes, expected_revision: int,
                  pool_version: str, shard_id: str, key_arn: str, user_key_id: str) -> None:
        """Create a new encrypted ENTRY and advance META in one transaction."""
        self._locator(locator)
        self._revision(expected_revision)
        validate_key_metadata(pool_version, shard_id, key_arn)
        validate_user_key_id(user_key_id)
        if not isinstance(purpose, str) or purpose not in self.allowed_purposes or not isinstance(epoch, str) or \
                not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", epoch) or int(epoch[:4]) < 1 or \
                not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= 400_000:
            raise ValueError("invalid encrypted capsule entry")
        self.client.transact_write_items(TransactItems=[
            {"Update": {"TableName": self.table_name, "Key": self._item({"pk": locator, "sk": "META"}),
                        "UpdateExpression": "SET #revision = :next",
                        "ConditionExpression": "#state = :active AND #revision = :expected AND user_key_id = :user_key "
                                               "AND pool_version = :pool AND shard_id = :shard AND key_arn = :key_arn "
                                               "AND attribute_exists(encrypted_user_dek)",
                        "ExpressionAttributeNames": {"#state": "state", "#revision": "revision"},
                        "ExpressionAttributeValues": self._item({":active": "ACTIVE", ":expected": expected_revision,
                            ":next": expected_revision + 1, ":user_key": user_key_id, ":pool": pool_version,
                            ":shard": shard_id, ":key_arn": key_arn})}},
            {"Put": {"TableName": self.table_name,
                     "Item": self._item({"pk": locator, "sk": f"ENTRY#{purpose}#{epoch}", "ciphertext": ciphertext,
                                         "pool_version": pool_version, "shard_id": shard_id, "key_arn": key_arn,
                                         "user_key_id": user_key_id}),
                     "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
        ])

    def attach_user_key(self, *, locator: str, expected_revision: int, user_key_id: str, encrypted_dek: bytes,
                        pool_version: str, shard_id: str, key_arn: str) -> None:
        """Attach one wrapped user key to an unlocked Capsule without overwriting it."""
        self._locator(locator)
        self._revision(expected_revision)
        validate_key_metadata(pool_version, shard_id, key_arn)
        validate_user_key_id(user_key_id)
        if not isinstance(encrypted_dek, bytes) or not 1 <= len(encrypted_dek) <= 8192:
            raise ValueError("invalid encrypted user key")
        self.client.update_item(TableName=self.table_name, Key=self._item({"pk": locator, "sk": "META"}),
            UpdateExpression="SET user_key_id = :user_key, encrypted_user_dek = :dek, pool_version = :pool, "
                             "shard_id = :shard, key_arn = :key_arn, #revision = :next",
            ConditionExpression="#state = :active AND #revision = :expected AND attribute_not_exists(user_key_id) "
                                "AND attribute_not_exists(encrypted_user_dek) AND attribute_not_exists(pool_version) "
                                "AND attribute_not_exists(shard_id) AND attribute_not_exists(key_arn)",
            ExpressionAttributeNames={"#state": "state", "#revision": "revision"},
            ExpressionAttributeValues=self._item({":active": "ACTIVE", ":expected": expected_revision,
                ":next": expected_revision + 1, ":user_key": user_key_id, ":dek": encrypted_dek,
                ":pool": pool_version, ":shard": shard_id, ":key_arn": key_arn}))

    def lock_action(self, *, locator: str, request_id: str, expected_revision: int) -> dict:
        """Build an update for the transaction that also locks the user and registers the tombstone.

        This action is deliberately not executed alone; the orchestration must
        bind it to an authoritative Vault version and the same deletion request.
        """
        self._locator(locator)
        self._revision(expected_revision)
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid deletion request ID") from None
        return {"Update": {"TableName": self.table_name, "Key": self._item({"pk": locator, "sk": "META"}),
                           "UpdateExpression": "SET #state = :locked, #revision = :next, deletion_request_id = :request",
                           "ConditionExpression": "#state = :active AND #revision = :expected AND attribute_not_exists(deletion_request_id)",
                           "ExpressionAttributeNames": {"#state": "state", "#revision": "revision"},
                           "ExpressionAttributeValues": self._item({":active": "ACTIVE", ":locked": "DELETION_LOCKED",
                               ":expected": expected_revision, ":next": expected_revision + 1, ":request": request_id})}}
