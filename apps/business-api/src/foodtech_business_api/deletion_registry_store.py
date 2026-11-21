"""Atomic deletion lock and encrypted tombstone registration.

This internal primitive is not a deletion endpoint or a complete deletion saga.
The caller must authenticate the owner and encrypt that owner's two locators
with the deletion-registry key and request-specific context before calling it.
"""
import re
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError


class DeletionRegistryConflict(Exception):
    pass


class DeletionRegistryStore:
    def __init__(self, registry_table: str, auth_table: str, *, client=None):
        if not registry_table or not auth_table or registry_table == auth_table:
            raise ValueError("separate registry and auth tables are required")
        self.registry_table, self.auth_table = registry_table, auth_table
        self.client = client or boto3.client("dynamodb", region_name="ap-southeast-2")
        self.serializer = TypeSerializer()
        self.deserializer = TypeDeserializer()

    def _item(self, value: dict) -> dict:
        return {name: self.serializer.serialize(field) for name, field in value.items()}

    def lock_and_register(self, *, subject: str, request_id: str, encrypted_user_id: bytes,
                          encrypted_capsule_locator: bytes, requested_at: str, policy_version: str) -> bool:
        """Return True on creation, False for an exact retry; raise on conflict.

        Both ciphertexts must already be encrypted for this owner and request.
        The same ciphertext bytes and metadata must be retained for retries.
        """
        try:
            if str(UUID(subject)) != subject or str(UUID(request_id)) != request_id:
                raise ValueError
            when = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            if when.tzinfo is None or when.utcoffset() != timezone.utc.utcoffset(when):
                raise ValueError
            if not isinstance(policy_version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", policy_version):
                raise ValueError
            if any(not isinstance(value, bytes) or not 1 <= len(value) <= 65536
                   for value in (encrypted_user_id, encrypted_capsule_locator)):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid deletion registry request") from None
        item = {"pk": "REGISTRY", "sk": f"REQ#{request_id}", "request_id": request_id,
                "requested_at": requested_at, "completed_at": None, "status": "DELETING",
                "encrypted_user_id": encrypted_user_id, "encrypted_capsule_locator": encrypted_capsule_locator,
                "reconcile_until": None, "expires_at": None, "ttl_delete_after": None,
                "policy_version": policy_version, "version": 1}
        principal_key = {"pk": f"USER#{subject}"}
        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": {"TableName": self.auth_table, "Key": self._item(principal_key),
                            "UpdateExpression": "SET suspended = :yes, deletion_lock_request = :request ADD #epoch :one",
                            "ConditionExpression": "attribute_not_exists(deletion_lock_request)",
                            "ExpressionAttributeNames": {"#epoch": "epoch"},
                            "ExpressionAttributeValues": self._item({":yes": True, ":request": request_id, ":one": 1})}},
                {"Put": {"TableName": self.registry_table, "Item": self._item(item),
                         "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
            ])
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            existing = self.client.get_item(TableName=self.registry_table,
                Key=self._item({"pk": "REGISTRY", "sk": f"REQ#{request_id}"}), ConsistentRead=True).get("Item")
            principal = self.client.get_item(TableName=self.auth_table, Key=self._item(principal_key),
                                             ConsistentRead=True).get("Item")
            expected = self._item(item)
            if existing and principal and principal.get("deletion_lock_request") == {"S": request_id} and \
                    principal.get("suspended") == {"BOOL": True} and \
                    all(existing.get(key) == expected[key] for key in
                        ("request_id", "requested_at", "encrypted_user_id", "encrypted_capsule_locator", "policy_version")):
                return False
            raise DeletionRegistryConflict("deletion lock or request already exists") from None

    def locked_request(self, subject: str) -> dict | None:
        """Recover nonsecret request state after an uncertain registration result.

        Caller authentication and a durable, authorized recovery workflow remain
        required. This does not resume deletion or read encrypted locators.
        """
        try:
            if str(UUID(subject)) != subject:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid deletion identity") from None
        key = self._item({"pk": f"USER#{subject}"})
        first = self.client.get_item(TableName=self.auth_table, Key=key, ConsistentRead=True).get("Item")
        if not first or "deletion_lock_request" not in first:
            return None
        request = first.get("deletion_lock_request", {}).get("S")
        try:
            epoch = self.deserializer.deserialize(first["epoch"])
            if first.get("suspended") != {"BOOL": True} or str(UUID(request)) != request or \
                    not isinstance(epoch, Decimal) or not epoch.is_finite() or epoch < 1 or epoch != int(epoch):
                raise ValueError
        except (ValueError, TypeError, AttributeError, KeyError, ArithmeticError):
            raise DeletionRegistryConflict("invalid deletion lock") from None
        record = self.client.get_item(TableName=self.registry_table,
            Key=self._item({"pk": "REGISTRY", "sk": f"REQ#{request}"}), ConsistentRead=True,
            ProjectionExpression="request_id, #status, #version, requested_at, policy_version",
            ExpressionAttributeNames={"#status": "status", "#version": "version"}).get("Item")
        second = self.client.get_item(TableName=self.auth_table, Key=key, ConsistentRead=True).get("Item")
        if not second or second.get("deletion_lock_request") != first.get("deletion_lock_request") or \
                second.get("suspended") != {"BOOL": True} or second.get("epoch") != first.get("epoch"):
            raise DeletionRegistryConflict("deletion lock changed during lookup")
        try:
            public = {name: self.deserializer.deserialize(record[name]) for name in
                      ("request_id", "status", "version", "requested_at", "policy_version")}
            if public["request_id"] != request or public["status"] not in {
                    "DELETING", "COMPLETED", "RECONCILING", "RECONCILE_FAILED"} or \
                    not isinstance(public["version"], Decimal) or not public["version"].is_finite() or \
                    public["version"] < 1 or public["version"] != int(public["version"]):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise DeletionRegistryConflict("missing or invalid deletion tombstone") from None
        public["version"] = int(public["version"])
        return public
