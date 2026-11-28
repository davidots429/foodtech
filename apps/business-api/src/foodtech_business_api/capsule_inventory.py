"""Read an encrypted Capsule item collection for an isolated deletion plan.

This is an internal storage adapter, not a tenant authorizer, decryptor, deletion
worker or proof that writers obey a deletion lock. Access requires a separately
authorized service and IAM role before integration.
"""
import re
from dataclasses import dataclass, field
from typing import Callable
from uuid import UUID

import boto3
from boto3.dynamodb.types import Binary, TypeDeserializer, TypeSerializer

from .capsule_metadata import validate_key_metadata, validate_tenant_scope, validate_user_key_id

class CapsuleInventoryError(Exception):
    pass


@dataclass(frozen=True)
class WrappedUserKey:
    user_key_id: str
    pool_version: str
    shard_id: str
    key_arn: str
    scope_purpose: str
    scope_epoch: str
    scope_key_version: str
    encrypted_dek: bytes = field(repr=False)


@dataclass(frozen=True)
class InventoryResult:
    revision: int
    entry_count: int
    wrapped_user_key: WrappedUserKey | None = field(repr=False)


class CapsuleInventory:
    def __init__(self, table_name: str, allowed_purposes: frozenset[str], *, client=None):
        if not table_name or not allowed_purposes or any(
                not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value)
                for value in allowed_purposes):
            raise ValueError("table and approved purpose inventory are required")
        self.table_name, self.allowed_purposes = table_name, allowed_purposes
        self.client = client or boto3.client("dynamodb", region_name="ap-southeast-2")
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()

    def _matches_scope(self, value: dict, request_id: str) -> bool:
        return value["state"] == "DELETION_LOCKED" and value.get("deletion_request_id") == request_id

    def _read_meta(self, locator: str, request_id: str) -> dict:
        item = self.client.get_item(TableName=self.table_name, ConsistentRead=True,
            Key={"pk": self.serializer.serialize(locator), "sk": self.serializer.serialize("META")},
            ProjectionExpression="pk, sk, #revision, #state, deletion_request_id, user_key_id, encrypted_user_dek, "
                                 "pool_version, shard_id, key_arn, scope_purpose, scope_epoch, scope_key_version",
            ExpressionAttributeNames={"#revision": "revision", "#state": "state"}).get("Item")
        if not item:
            raise CapsuleInventoryError("capsule metadata is unavailable")
        try:
            value = {name: self.deserializer.deserialize(item[name]) for name in
                     ("pk", "sk", "revision", "state", "deletion_request_id") if name in item}
            revision = value["revision"]
            if value["pk"] != locator or value["sk"] != "META" or not self._matches_scope(value, request_id) or \
                    not revision.is_finite() or revision < 1 or revision != int(revision):
                raise ValueError
            key_fields = ("user_key_id", "encrypted_user_dek", "pool_version", "shard_id", "key_arn",
                          "scope_purpose", "scope_epoch", "scope_key_version")
            present = [name in item for name in key_fields]
            if any(present):
                if not all(present):
                    raise ValueError
                for name in key_fields:
                    value[name] = self.deserializer.deserialize(item[name])
                if isinstance(value["encrypted_user_dek"], Binary):
                    value["encrypted_user_dek"] = bytes(value["encrypted_user_dek"])
                validate_user_key_id(value["user_key_id"])
                validate_key_metadata(value["pool_version"], value["shard_id"], value["key_arn"])
                validate_tenant_scope(value["scope_purpose"], value["scope_epoch"], value["scope_key_version"])
                if not isinstance(value["encrypted_user_dek"], bytes) or not 1 <= len(value["encrypted_user_dek"]) <= 8192:
                    raise ValueError
            value["revision"] = int(revision)
            return value
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise CapsuleInventoryError("invalid capsule metadata") from None

    def stage(self, locator: str, request_id: str, stage_entry: Callable[[str, bytes, dict[str, str]], None]) -> InventoryResult:
        """Stage each encrypted ENTRY; return success only after all pages and a stable revision.

        ``stage_entry`` may persist a provisional plan but must not delete data or
        mark the plan complete. A caller must discard that plan on any exception.
        The wrapped key is returned only after the full inventory and second META
        check succeed; it must remain within the authorized deletion boundary.
        The service must independently prevent all Capsule writers while locked and
        enforce revision increments for every change; this method cannot prove it.
        """
        try:
            if not isinstance(locator, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", locator) or \
                    str(UUID(request_id)) != request_id or not callable(stage_entry):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError("invalid capsule inventory request") from None
        before = self._read_meta(locator, request_id)
        previous = ""
        count = 0
        page_count = 0
        paginator = self.client.get_paginator("query")
        for page in paginator.paginate(TableName=self.table_name, ConsistentRead=True,
                KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={":pk": self.serializer.serialize(locator),
                                           ":prefix": self.serializer.serialize("ENTRY#")},
                ProjectionExpression="pk, sk, ciphertext, pool_version, shard_id, key_arn, user_key_id"):
            page_count += 1
            if not isinstance(page.get("Items"), list):
                raise CapsuleInventoryError("invalid capsule query page")
            for item in page["Items"]:
                try:
                    pk = self.deserializer.deserialize(item["pk"])
                    sk = self.deserializer.deserialize(item["sk"])
                    ciphertext = self.deserializer.deserialize(item["ciphertext"])
                    key_metadata = {name: self.deserializer.deserialize(item[name]) for name in
                                    ("pool_version", "shard_id", "key_arn")}
                    validate_key_metadata(**key_metadata)
                    key_metadata["user_key_id"] = self.deserializer.deserialize(item["user_key_id"])
                    validate_user_key_id(key_metadata["user_key_id"])
                    if any(key_metadata[name] != before.get(name) for name in key_metadata):
                        raise ValueError
                    if isinstance(ciphertext, Binary):
                        ciphertext = bytes(ciphertext)
                    match = re.fullmatch(r"ENTRY#([A-Z][A-Z0-9_]{0,63})#([0-9]{4}-(?:0[1-9]|1[0-2]))", sk)
                    if pk != locator or sk <= previous or not match or int(match.group(2)[:4]) < 1 or \
                            match.group(1) not in self.allowed_purposes or \
                            not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= 400_000:
                        raise ValueError
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise CapsuleInventoryError("invalid encrypted capsule entry") from None
                stage_entry(sk, ciphertext, key_metadata)
                previous = sk
                count += 1
        if not page_count:
            raise CapsuleInventoryError("capsule query returned no pages")
        after = self._read_meta(locator, request_id)
        if before != after:
            raise CapsuleInventoryError("capsule changed during inventory")
        wrapped_key = None
        if "user_key_id" in before:
            wrapped_key = WrappedUserKey(user_key_id=before["user_key_id"], pool_version=before["pool_version"],
                shard_id=before["shard_id"], key_arn=before["key_arn"], scope_purpose=before["scope_purpose"],
                scope_epoch=before["scope_epoch"], scope_key_version=before["scope_key_version"],
                encrypted_dek=before["encrypted_user_dek"])
        return InventoryResult(revision=before["revision"], entry_count=count, wrapped_user_key=wrapped_key)


class RestoredCapsuleInventory(CapsuleInventory):
    """Read only the Link Capsule table of a specific isolated recovery set."""

    def __init__(self, restored_table_arn: str, allowed_purposes: frozenset[str], *, recovery_id: str, client):
        try:
            if not isinstance(recovery_id, str) or str(UUID(recovery_id)) != recovery_id:
                raise ValueError
        except ValueError:
            raise ValueError("explicit Capsule recovery identifier required") from None
        pattern = rf"arn:aws:dynamodb:ap-southeast-2:[0-9]{{12}}:table/foodtech-link-capsule-restore-{recovery_id}"
        if not isinstance(restored_table_arn, str) or not re.fullmatch(pattern, restored_table_arn) or client is None:
            raise ValueError("explicit isolated Capsule table and recovery client required")
        super().__init__(restored_table_arn, allowed_purposes, client=client)
        self.restored_table_arn = restored_table_arn

    def _matches_scope(self, value: dict, request_id: str) -> bool:
        return (value["state"] == "ACTIVE" and "deletion_request_id" not in value
                or super()._matches_scope(value, request_id))
