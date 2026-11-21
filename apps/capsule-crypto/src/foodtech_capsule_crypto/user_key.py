"""User-scoped KMS-wrapped AES key; access requires a separately scoped role.

This module does not select a tenant, issue STS tags, persist keys, or execute
deletion. Callbacks are internal trusted code and must not copy or expose keys.
"""
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import boto3

from .contract import Envelope, context


@dataclass(frozen=True)
class UserKeyRecord:
    key_id: str
    pool_version: str
    shard_id: str
    key_arn: str
    encrypted_dek: bytes = field(repr=False)

    def validate(self, key_map: dict[tuple[str, str], str]) -> None:
        try:
            if str(UUID(self.key_id)) != self.key_id:
                raise ValueError
            Envelope(self.pool_version, self.shard_id, self.key_arn, self.encrypted_dek).validate(key_map)
            if len(self.encrypted_dek) > 8192:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid user key record") from None


def _key_context(tenant: str, pool_version: str, shard_id: str, purposes: frozenset[str]) -> dict[str, str]:
    return {**context(tenant, pool_version, shard_id, purposes), "purpose": "CAPSULE_USER_KEY"}


class UserKeyGenerator:
    def __init__(self, key_map: dict[tuple[str, str], str], approved_tenant_purposes: frozenset[str], *, kms_client=None):
        if not key_map or not approved_tenant_purposes:
            raise ValueError("trusted key map and tenant purposes are required")
        self.key_map, self.purposes = dict(key_map), frozenset(approved_tenant_purposes)
        self.kms = kms_client or boto3.client("kms", region_name="ap-southeast-2")

    def generate(self, *, tenant: str, pool_version: str, shard_id: str, use_key):
        """Return wrapped metadata and callback result, clearing the mutable key buffer."""
        key = bytearray()
        try:
            if not callable(use_key):
                raise ValueError
            key_arn = self.key_map.get((pool_version, shard_id), "")
            aad = _key_context(tenant, pool_version, shard_id, self.purposes)
            Envelope(pool_version, shard_id, key_arn, b"x").validate(self.key_map)
            response = self.kms.generate_data_key(KeyId=key_arn, KeySpec="AES_256", EncryptionContext=aad)
            key.extend(response["Plaintext"])
            record = UserKeyRecord(str(uuid4()), pool_version, shard_id, key_arn, response["CiphertextBlob"])
            record.validate(self.key_map)
            if response.get("KeyId") != key_arn or len(key) != 32:
                raise ValueError
            return record, use_key(key, record)
        except Exception:
            raise ValueError("unable to create capsule user key") from None
        finally:
            key[:] = b"\x00" * len(key)


class UserKeyUnwrapper:
    def __init__(self, key_map: dict[tuple[str, str], str], approved_tenant_purposes: frozenset[str], *, kms_client=None):
        if not key_map or not approved_tenant_purposes:
            raise ValueError("trusted key map and tenant purposes are required")
        self.key_map, self.purposes = dict(key_map), frozenset(approved_tenant_purposes)
        self.kms = kms_client or boto3.client("kms", region_name="ap-southeast-2")

    def use(self, *, tenant: str, record: UserKeyRecord, use_key):
        """Unwrap only inside this restricted boundary and clear its mutable key buffer."""
        key = bytearray()
        try:
            if not callable(use_key):
                raise ValueError
            record.validate(self.key_map)
            aad = _key_context(tenant, record.pool_version, record.shard_id, self.purposes)
            response = self.kms.decrypt(KeyId=record.key_arn, CiphertextBlob=record.encrypted_dek,
                                        EncryptionContext=aad, EncryptionAlgorithm="SYMMETRIC_DEFAULT")
            key.extend(response["Plaintext"])
            if response.get("KeyId") != record.key_arn or len(key) != 32:
                raise ValueError
            return use_key(key, record)
        except Exception:
            raise ValueError("unable to unwrap capsule user key") from None
        finally:
            key[:] = b"\x00" * len(key)
