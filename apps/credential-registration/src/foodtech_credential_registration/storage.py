"""Encrypt and write a credential without a stored-secret read or decrypt path."""

from typing import Any

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateAwsKmsKeyringInput
from aws_encryption_sdk import CommitmentPolicy
from aws_encryption_sdk.identifiers import Algorithm
from boto3.dynamodb.conditions import Attr


SUITE = Algorithm.AES_256_GCM_HKDF_SHA512_COMMIT_KEY_ECDSA_P384


class CredentialWriter:
    def __init__(self, table_name: str, environment: str, key_arn: str,
                 *, resource: Any = None, kms_client: Any = None, sdk_client: Any = None,
                 material_providers: Any = None):
        if not table_name or not environment or not key_arn.startswith("arn:aws:kms:"):
            raise ValueError("vault table, environment and server-selected KMS key ARN are required")
        self.table = (resource or boto3.resource("dynamodb")).Table(table_name)
        self.environment = environment
        self.key_arn = key_arn
        self.kms_client = kms_client or boto3.client("kms")
        self.sdk = sdk_client or aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT,
            max_encrypted_data_keys=1)
        self.material_providers = material_providers or AwsCryptographicMaterialProviders(
            config=MaterialProvidersConfig())

    def store(self, credential_id: str, tenant_pseudonym: str, provider: str,
              plaintext: bytearray, *, pool_version: str, shard_id: str) -> None:
        item = self.encrypt_item(credential_id, tenant_pseudonym, provider, plaintext,
                                 pool_version=pool_version, shard_id=shard_id)
        self.table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())

    def encrypt_item(self, credential_id: str, tenant_pseudonym: str, provider: str,
                     plaintext: bytearray, *, pool_version: str, shard_id: str) -> dict:
        try:
            if not credential_id or len(credential_id) > 256 or not tenant_pseudonym or \
                    provider not in {"openai", "google", "anthropic"} or \
                    not pool_version or not shard_id or not 1 <= len(plaintext) <= 400:
                raise ValueError("invalid credential metadata or key byte length")
            context = {
                "tenant_pseudonym": tenant_pseudonym,
                "credential_id": credential_id,
                "purpose": "EXTERNAL_AI_CREDENTIAL",
                "provider": provider,
                "environment": self.environment,
                "schema_version": "1",
                "pool_version": pool_version,
                "shard_id": shard_id,
            }
            keyring = self.material_providers.create_aws_kms_keyring(input=CreateAwsKmsKeyringInput(
                kms_key_id=self.key_arn, kms_client=self.kms_client))
            ciphertext, header = self.sdk.encrypt(source=bytes(plaintext), keyring=keyring,
                                                  encryption_context=context, algorithm=SUITE)
            if header.algorithm != SUITE:
                raise ValueError("unsupported credential encryption suite")
            return {
                "pk": f"CREDENTIAL#{credential_id}", "sk": "META", "state": "ACTIVE",
                "credential_id": credential_id, "tenant_pseudonym": tenant_pseudonym,
                "provider": provider, "environment": self.environment,
                "schema_version": "1", "pool_version": pool_version,
                "shard_id": shard_id, "key_arn": self.key_arn,
                # The AWS Encryption SDK message includes the encrypted data key.
                "ciphertext": ciphertext,
            }
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
