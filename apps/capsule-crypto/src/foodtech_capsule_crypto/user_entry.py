"""Use a caller-scoped user key to wrap each SDK-generated entry data key.

Only a restricted key boundary may call these helpers. They do not authenticate
the user, prove STS tags or authorize access to a Capsule.
"""
from dataclasses import dataclass, field
from uuid import UUID

from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import AesWrappingAlg, CreateRawAesKeyringInput

from .contract import Entry, Envelope
from .reader import CapsuleEntryReader
from .user_key import UserKeyRecord
from .writer import CapsuleEntryWriter


@dataclass(frozen=True)
class UserEntryEnvelope:
    user_key_id: str
    entry: Envelope = field(repr=False)


def _factory(key: bytearray, record: UserKeyRecord, key_map, providers):
    record.validate(key_map)
    if not isinstance(key, bytearray) or len(key) != 32 or str(UUID(record.key_id)) != record.key_id:
        raise ValueError("invalid capsule user key")
    def create(key_arn):
        if key_arn != record.key_arn:
            raise ValueError("capsule key mismatch")
        return providers.create_raw_aes_keyring(input=CreateRawAesKeyringInput(
            key_namespace="foodtech-capsule-user-key-v1", key_name=record.key_id,
            wrapping_key=bytes(key), wrapping_alg=AesWrappingAlg.ALG_AES256_GCM_IV12_TAG16))
    return create


def seal_entry(*, key: bytearray, record: UserKeyRecord, key_map, tenant: str, entry: Entry,
               approved_purposes: frozenset[str], approved_tenant_purposes: frozenset[str],
               sdk=None, providers=None, kms_client=None) -> UserEntryEnvelope:
    """Encrypt with the in-scope user key; the SDK creates a fresh message key."""
    materials = providers or AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())
    writer = CapsuleEntryWriter(approved_purposes, key_map, approved_tenant_purposes=approved_tenant_purposes,
        sdk=sdk, providers=materials, kms_client=kms_client, keyring_factory=_factory(key, record, key_map, materials))
    envelope = writer.seal(tenant_pseudonym=tenant, entry=entry, pool_version=record.pool_version,
                           shard_id=record.shard_id)
    return UserEntryEnvelope(record.key_id, envelope)


def open_entry(*, key: bytearray, record: UserKeyRecord, key_map, tenant: str, envelope: UserEntryEnvelope,
               expected_purpose: str, expected_epoch: str, approved_purposes: frozenset[str],
               approved_tenant_purposes: frozenset[str], sdk=None, providers=None, kms_client=None) -> Entry:
    """Open only when the encrypted entry references this user's wrapped key."""
    if not isinstance(envelope, UserEntryEnvelope) or envelope.user_key_id != record.key_id or \
            envelope.entry.pool_version != record.pool_version or envelope.entry.shard_id != record.shard_id or \
            envelope.entry.key_arn != record.key_arn:
        raise ValueError("capsule user key reference mismatch")
    materials = providers or AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())
    reader = CapsuleEntryReader(approved_purposes, key_map, approved_tenant_purposes=approved_tenant_purposes,
        sdk=sdk, providers=materials, kms_client=kms_client, keyring_factory=_factory(key, record, key_map, materials))
    return reader.open(tenant_pseudonym=tenant, envelope=envelope.entry,
                       expected_purpose=expected_purpose, expected_epoch=expected_epoch)
