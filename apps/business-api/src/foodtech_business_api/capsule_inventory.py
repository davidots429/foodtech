"""Read an encrypted Capsule item collection for an isolated deletion plan.

This is an internal storage adapter, not a tenant authorizer, decryptor, deletion
worker or proof that writers obey a deletion lock. Access requires a separately
authorized service and IAM role before integration.
"""
import re
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

import boto3
from boto3.dynamodb.types import Binary, TypeDeserializer, TypeSerializer

from .capsule_metadata import validate_key_metadata

class CapsuleInventoryError(Exception):
    pass


@dataclass(frozen=True)
class InventoryResult:
    revision: int
    entry_count: int


class CapsuleInventory:
    def __init__(self, table_name: str, allowed_purposes: frozenset[str], *, client=None):
        if not table_name or not allowed_purposes or any(
                not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value)
                for value in allowed_purposes):
            raise ValueError("table and approved purpose inventory are required")
        self.table_name, self.allowed_purposes = table_name, allowed_purposes
        self.client = client or boto3.client("dynamodb", region_name="ap-southeast-2")
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()

    def _read_meta(self, locator: str, request_id: str) -> dict:
        item = self.client.get_item(TableName=self.table_name, ConsistentRead=True,
            Key={"pk": self.serializer.serialize(locator), "sk": self.serializer.serialize("META")},
            ProjectionExpression="pk, sk, #revision, #state, deletion_request_id",
            ExpressionAttributeNames={"#revision": "revision", "#state": "state"}).get("Item")
        if not item:
            raise CapsuleInventoryError("capsule metadata is unavailable")
        try:
            value = {name: self.deserializer.deserialize(item[name]) for name in
                     ("pk", "sk", "revision", "state", "deletion_request_id")}
            revision = value["revision"]
            if value["pk"] != locator or value["sk"] != "META" or value["state"] != "DELETION_LOCKED" or \
                    value["deletion_request_id"] != request_id or \
                    not revision.is_finite() or revision < 1 or revision != int(revision):
                raise ValueError
            value["revision"] = int(revision)
            return value
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise CapsuleInventoryError("invalid capsule metadata") from None

    def stage(self, locator: str, request_id: str, stage_entry: Callable[[str, bytes, dict[str, str]], None]) -> InventoryResult:
        """Stage each encrypted ENTRY; return success only after all pages and a stable revision.

        ``stage_entry`` may persist a provisional plan but must not delete data or
        mark the plan complete. A caller must discard that plan on any exception.
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
                ProjectionExpression="pk, sk, ciphertext, pool_version, shard_id, key_arn"):
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
        return InventoryResult(revision=before["revision"], entry_count=count)
