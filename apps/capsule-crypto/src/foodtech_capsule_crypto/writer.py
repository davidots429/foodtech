"""Encrypt one Capsule entry; no decrypt or persistence operation is exposed."""
import json

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_encryption_sdk import CommitmentPolicy
from aws_encryption_sdk.identifiers import Algorithm

from .contract import Entry, Envelope, context, header_matches


SUITE = Algorithm.AES_256_GCM_HKDF_SHA512_COMMIT_KEY_ECDSA_P384


class CapsuleEntryWriter:
    def __init__(self, approved_purposes: frozenset[str], key_map: dict[tuple[str, str], str], *,
                 approved_tenant_purposes: frozenset[str],
                 kms_client=None, sdk=None, providers=None, keyring_factory=None):
        if not approved_purposes or not approved_tenant_purposes or not key_map or not callable(keyring_factory):
            raise ValueError("approved purposes and trusted key map are required")
        self.approved_purposes, self.key_map = frozenset(approved_purposes), dict(key_map)
        self.approved_tenant_purposes = frozenset(approved_tenant_purposes)
        self.kms = kms_client or boto3.client("kms", region_name="ap-southeast-2")
        self.sdk = sdk or aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT, max_encrypted_data_keys=1)
        self.providers = providers or AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())
        self.keyring_factory = keyring_factory

    def seal(self, *, tenant_pseudonym: str, entry: Entry, pool_version: str, shard_id: str) -> Envelope:
        """Use the server-selected pool key; the SDK generates an entry-specific data key."""
        plaintext = bytearray()
        try:
            entry.validate(self.approved_purposes)
            aad = context(tenant_pseudonym, pool_version, shard_id, self.approved_tenant_purposes)
            key_arn = self.key_map.get((pool_version, shard_id), "")
            Envelope(pool_version, shard_id, key_arn, b"x").validate(self.key_map)
            plaintext.extend(json.dumps({"schema_version": "1", "purpose": entry.purpose, "epoch": entry.epoch,
                                         "pseudonym_digest": entry.pseudonym_digest},
                                        ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            keyring = self.keyring_factory(key_arn)
            ciphertext, header = self.sdk.encrypt(source=bytes(plaintext), keyring=keyring,
                                                   encryption_context=aad, algorithm=SUITE)
            if header.algorithm != SUITE or not header_matches(header.encryption_context, aad):
                raise ValueError("invalid capsule encryption")
            envelope = Envelope(pool_version, shard_id, key_arn, ciphertext)
            envelope.validate(self.key_map)
            return envelope
        except Exception:
            raise ValueError("unable to encrypt capsule entry") from None
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
