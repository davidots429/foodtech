"""Registration-only transport decryption and atomic credential metadata write."""

import base64
import binascii
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer

from .storage import CredentialWriter


class RegistrationError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TransferKey:
    def __init__(self, key_arn: str, version: str, *, kms_client: Any = None):
        if not key_arn.startswith("arn:aws:kms:") or not version:
            raise ValueError("active transfer key ARN and version are required")
        self.key_arn = key_arn
        self.version = version
        self.kms = kms_client or boto3.client("kms")
        self._cached: tuple[float, dict] | None = None

    def public_descriptor(self) -> dict:
        now = time.time()
        if self._cached and self._cached[0] > now:
            return self._cached[1]
        result = self.kms.get_public_key(KeyId=self.key_arn)
        if result.get("KeySpec") != "RSA_4096" or result.get("KeyUsage") != "ENCRYPT_DECRYPT" or \
                "RSAES_OAEP_SHA_256" not in result.get("EncryptionAlgorithms", []):
            raise RegistrationError("TRANSFER_KEY_INVALID")
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        descriptor = {"key_version": self.version, "algorithm": "RSA-OAEP-SHA-256",
                      "public_key_spki_base64": base64.b64encode(result["PublicKey"]).decode("ascii"),
                      "expires_at": expires.isoformat()}
        self._cached = (now + 600, descriptor)
        return descriptor

    def decrypt(self, version: str, ciphertext_base64: str) -> bytearray:
        if version != self.version or len(ciphertext_base64) > 1024:
            raise RegistrationError("TRANSFER_KEY_VERSION_INVALID")
        try:
            ciphertext = base64.b64decode(ciphertext_base64, validate=True)
        except (binascii.Error, ValueError):
            raise RegistrationError("CIPHERTEXT_INVALID") from None
        if len(ciphertext) != 512:
            raise RegistrationError("CIPHERTEXT_INVALID")
        try:
            plaintext = self.kms.decrypt(KeyId=self.key_arn, CiphertextBlob=ciphertext,
                                         EncryptionAlgorithm="RSAES_OAEP_SHA_256")["Plaintext"]
        except Exception:
            raise RegistrationError("TRANSFER_DECRYPT_FAILED") from None
        if not 1 <= len(plaintext) <= 400:
            raise RegistrationError("CREDENTIAL_LENGTH_INVALID")
        try:
            plaintext.decode("utf-8")
        except UnicodeDecodeError:
            raise RegistrationError("CREDENTIAL_ENCODING_INVALID") from None
        return bytearray(plaintext)


class TenantPseudonymizer:
    def __init__(self, key_arn: str, key_version: str, *, kms_client: Any = None):
        if not key_arn.startswith("arn:aws:kms:") or not key_version:
            raise ValueError("HMAC key ARN and version are required")
        self.key_arn = key_arn
        self.key_version = key_version
        self.kms = kms_client or boto3.client("kms")

    def create(self, subject: str, epoch: str) -> str:
        if not subject or len(subject) > 128 or len(epoch) != 7 or epoch[4] != "-":
            raise RegistrationError("TENANT_SCOPE_INVALID")
        parts = ("v1", "credential", epoch, self.key_version, subject)
        message = b"".join(len(value.encode("utf-8")).to_bytes(4, "big") + value.encode("utf-8")
                           for value in parts)
        mac = self.kms.generate_mac(KeyId=self.key_arn, Message=message,
                                    MacAlgorithm="HMAC_SHA_256")["Mac"]
        digest = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
        return f"psn:v1:credential:{epoch}:{self.key_version}:{digest}"


class CredentialRegistrar:
    def __init__(self, writer: CredentialWriter, registry_table: str, transfer: TransferKey,
                 pseudonymizer: TenantPseudonymizer, *, dynamodb_client: Any = None):
        if not registry_table:
            raise ValueError("AI registry table is required")
        self.writer = writer
        self.registry_table = registry_table
        self.transfer = transfer
        self.pseudonymizer = pseudonymizer
        self.client = dynamodb_client or boto3.client("dynamodb")
        self.serializer = TypeSerializer()

    def _item(self, value: dict) -> dict:
        return {key: self.serializer.serialize(item) for key, item in value.items()}

    def register(self, tenant_id: str, provider: str, key_version: str,
                 ciphertext_base64: str) -> str:
        if provider not in {"openai", "google", "anthropic"}:
            raise RegistrationError("PROVIDER_INVALID")
        plaintext = self.transfer.decrypt(key_version, ciphertext_base64)
        try:
            epoch = datetime.now(timezone.utc).strftime("%Y-%m")
            pseudonym = self.pseudonymizer.create(tenant_id, epoch)
            credential_id = str(uuid4())
            vault_item = self.writer.encrypt_item(credential_id, pseudonym, provider, plaintext,
                                                  pool_version=epoch, shard_id="0")
            metadata = {"pk": f"CREDENTIAL#{credential_id}", "sk": "META",
                        "credential_id": credential_id, "tenant_id": tenant_id,
                        "tenant_pseudonym": pseudonym, "provider": provider,
                        "state": "ACTIVE", "expires_at": None,
                        "vault_ref": f"vault:{credential_id}", "version": 1}
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.writer.table.name, "Item": self._item(vault_item),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Put": {"TableName": self.registry_table, "Item": self._item(metadata),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ])
            return credential_id
        finally:
            plaintext[:] = b"\x00" * len(plaintext)


@lru_cache(maxsize=1)
def from_env() -> CredentialRegistrar:
    vault_table = os.environ["FOODTECH_CREDENTIAL_VAULT_TABLE"]
    storage_key = os.environ["FOODTECH_CREDENTIAL_STORAGE_KEY_ARN"]
    registry_table = os.environ["FOODTECH_AI_REGISTRY_TABLE"]
    transfer_key = os.environ["FOODTECH_CREDENTIAL_TRANSFER_KEY_ARN"]
    transfer_version = os.environ["FOODTECH_CREDENTIAL_TRANSFER_KEY_VERSION"]
    hmac_key = os.environ["FOODTECH_CREDENTIAL_HMAC_KEY_ARN"]
    hmac_version = os.environ["FOODTECH_CREDENTIAL_HMAC_KEY_VERSION"]
    environment = os.getenv("FOODTECH_STAGE", "dev")
    return CredentialRegistrar(
        CredentialWriter(vault_table, environment, storage_key), registry_table,
        TransferKey(transfer_key, transfer_version), TenantPseudonymizer(hmac_key, hmac_version))
