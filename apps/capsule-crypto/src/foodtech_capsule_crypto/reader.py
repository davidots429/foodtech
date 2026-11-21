"""Restricted Capsule decryption primitive; IAM/JIT must be enforced externally."""
import json

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_encryption_sdk import CommitmentPolicy

from .contract import Entry, Envelope, context, header_matches
from .writer import SUITE


class CapsuleEntryReader:
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

    def open(self, *, tenant_pseudonym: str, envelope: Envelope, expected_purpose: str, expected_epoch: str) -> Entry:
        """Verify key, authenticated context and entry identity before returning it."""
        plaintext = bytearray()
        try:
            envelope.validate(self.key_map)
            aad = context(tenant_pseudonym, envelope.pool_version, envelope.shard_id, self.approved_tenant_purposes)
            keyring = self.keyring_factory(envelope.key_arn)
            raw, header = self.sdk.decrypt(source=envelope.ciphertext, keyring=keyring, encryption_context=aad)
            plaintext.extend(raw)
            if header.algorithm != SUITE or not header_matches(header.encryption_context, aad) or \
                    not 1 <= len(plaintext) <= 2048:
                raise ValueError("invalid capsule ciphertext")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result
            payload = json.loads(plaintext.decode("utf-8"), object_pairs_hook=unique)
            if not isinstance(payload, dict) or set(payload) != {"schema_version", "purpose", "epoch", "pseudonym_digest"} or \
                    payload["schema_version"] != "1":
                raise ValueError("invalid capsule payload")
            entry = Entry(payload["purpose"], payload["epoch"], payload["pseudonym_digest"])
            entry.validate(self.approved_purposes)
            if entry.purpose != expected_purpose or entry.epoch != expected_epoch:
                raise ValueError("capsule entry identity mismatch")
            return entry
        except Exception:
            raise ValueError("unable to decrypt capsule entry") from None
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
