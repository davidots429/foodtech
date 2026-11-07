"""Gateway-only read and decrypt path for stored external AI credentials."""

from dataclasses import dataclass
from typing import Any

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateAwsKmsKeyringInput
from aws_encryption_sdk import CommitmentPolicy
from aws_encryption_sdk.identifiers import Algorithm

from .policy import Credential


SUITE = Algorithm.AES_256_GCM_HKDF_SHA512_COMMIT_KEY_ECDSA_P384
PURPOSE = "EXTERNAL_AI_CREDENTIAL"


class VaultFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VaultRecord:
    credential_id: str
    tenant_pseudonym: str
    provider: str
    environment: str
    pool_version: str
    shard_id: str
    key_arn: str
    ciphertext: bytes

    def context(self) -> dict[str, str]:
        return {
            "tenant_pseudonym": self.tenant_pseudonym,
            "credential_id": self.credential_id,
            "purpose": PURPOSE,
            "provider": self.provider,
            "environment": self.environment,
            "schema_version": "1",
            "pool_version": self.pool_version,
            "shard_id": self.shard_id,
        }


class DynamoCredentialVault:
    def __init__(self, table_name: str, environment: str, allowed_key_arns: set[str],
                 *, resource: Any = None, kms_client: Any = None, sdk_client: Any = None,
                 material_providers: Any = None):
        if not table_name or not environment or not allowed_key_arns or any(
            not key.startswith("arn:aws:kms:") for key in allowed_key_arns
        ):
            raise ValueError("vault table, environment and KMS key allowlist are required")
        self.table = (resource or boto3.resource("dynamodb")).Table(table_name)
        self.environment = environment
        self.allowed_key_arns = frozenset(allowed_key_arns)
        self.kms_client = kms_client or boto3.client("kms")
        self.sdk = sdk_client or aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT,
            max_encrypted_data_keys=1)
        self.material_providers = material_providers or AwsCryptographicMaterialProviders(
            config=MaterialProvidersConfig())

    def read(self, credential: Credential) -> VaultRecord:
        if not credential.tenant_pseudonym or credential.vault_ref != (
            f"vault:{credential.credential_id}"
        ):
            raise VaultFailure("CREDENTIAL_REFERENCE_INVALID")
        item = self.table.get_item(Key={"pk": f"CREDENTIAL#{credential.credential_id}",
                                        "sk": "META"}, ConsistentRead=True).get("Item")
        if not item or item.get("state") != "ACTIVE" or item.get("credential_id") != credential.credential_id or \
                item.get("tenant_pseudonym") != credential.tenant_pseudonym or \
                item.get("provider") != credential.provider or \
                item.get("environment") != self.environment:
            raise VaultFailure("CREDENTIAL_VAULT_MISMATCH")
        key_arn = item.get("key_arn")
        if key_arn not in self.allowed_key_arns:
            raise VaultFailure("CREDENTIAL_KEY_NOT_ALLOWED")
        if item.get("schema_version") != "1" or not item.get("pool_version") or \
                not item.get("shard_id") or not item.get("ciphertext"):
            raise VaultFailure("CREDENTIAL_VAULT_INVALID")
        return VaultRecord(credential.credential_id, credential.tenant_pseudonym,
                           credential.provider, self.environment, item["pool_version"],
                           item["shard_id"], key_arn, bytes(item["ciphertext"]))

    def decrypt(self, record: VaultRecord) -> bytearray:
        if record.key_arn not in self.allowed_key_arns or record.environment != self.environment:
            raise VaultFailure("CREDENTIAL_KEY_NOT_ALLOWED")
        try:
            keyring = self.material_providers.create_aws_kms_keyring(input=CreateAwsKmsKeyringInput(
                kms_key_id=record.key_arn, kms_client=self.kms_client))
            plaintext, header = self.sdk.decrypt(source=record.ciphertext, keyring=keyring,
                                                 encryption_context=record.context())
            if header.algorithm != SUITE or any(
                header.encryption_context.get(key) != value
                for key, value in record.context().items()
            ) or not 1 <= len(plaintext) <= 400:
                raise VaultFailure("CREDENTIAL_CIPHERTEXT_INVALID")
            return bytearray(plaintext)
        except VaultFailure:
            raise
        except Exception:
            raise VaultFailure("CREDENTIAL_DECRYPT_FAILED") from None
