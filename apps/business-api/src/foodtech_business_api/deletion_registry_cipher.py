"""Write-only encryption boundary for deletion registry recovery locators.

This component does not register tombstones or establish a deletion lock. The
separate recovery role must own decryption; no decrypt operation is exposed here.
"""
import re
from uuid import UUID

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateAwsKmsKeyringInput
from aws_encryption_sdk import CommitmentPolicy
from aws_encryption_sdk.identifiers import Algorithm


SUITE = Algorithm.AES_256_GCM_HKDF_SHA512_COMMIT_KEY_ECDSA_P384
MAX_PLAINTEXT_BYTES = 4096
MAX_CIPHERTEXT_BYTES = 65536


class DeletionRegistryCipher:
    """Encrypt two mutable buffers independently for one deletion request."""

    def __init__(self, key_arn: str, environment: str, *, kms_client=None, sdk=None, providers=None):
        if not isinstance(key_arn, str) or not re.fullmatch(
                r"arn:aws:kms:ap-southeast-2:[0-9]{12}:key/[A-Za-z0-9-]+", key_arn) or \
                not isinstance(environment, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", environment):
            raise ValueError("invalid deletion registry key configuration")
        self.key_arn = key_arn
        self.environment = environment
        self.kms = kms_client or boto3.client("kms", region_name="ap-southeast-2")
        self.sdk = sdk or aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT, max_encrypted_data_keys=1)
        self.providers = providers or AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())

    def seal(self, request_id: str, internal_id: bytearray, capsule_locator: bytearray) -> dict[str, bytes]:
        """Return only ciphertext and erase mutable input buffers even on failure."""
        try:
            if not isinstance(internal_id, bytearray) or not isinstance(capsule_locator, bytearray) or \
                    internal_id is capsule_locator:
                raise ValueError("distinct mutable buffers required")
            parsed = UUID(request_id)
            if str(parsed) != request_id or not all(1 <= len(value) <= MAX_PLAINTEXT_BYTES
                                                      for value in (internal_id, capsule_locator)):
                raise ValueError("invalid deletion locator")
            keyring = self.providers.create_aws_kms_keyring(input=CreateAwsKmsKeyringInput(
                kms_key_id=self.key_arn, kms_client=self.kms))
            encrypted = {}
            for field, value in (("internal_user_id", internal_id), ("capsule_locator", capsule_locator)):
                context = {"purpose": "DELETION_REGISTRY", "environment": self.environment,
                           "schema_version": "1", "request_id": request_id, "field": field}
                ciphertext, header = self.sdk.encrypt(source=bytes(value), keyring=keyring,
                                                       encryption_context=context, algorithm=SUITE)
                if header.algorithm != SUITE or not isinstance(ciphertext, bytes) or \
                        not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
                    raise ValueError("invalid ciphertext")
                encrypted[field] = ciphertext
            return encrypted
        except Exception:
            raise ValueError("unable to encrypt deletion registry locators") from None
        finally:
            for value in (internal_id, capsule_locator):
                if isinstance(value, bytearray):
                    value[:] = b"\x00" * len(value)
