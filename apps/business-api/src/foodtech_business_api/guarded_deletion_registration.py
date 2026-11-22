"""Internal owner-bound preparation for guarded deletion registration.

The resolver must enforce owner access and return a consistent Vault/Capsule
snapshot. No public API, scoped role, durable resume or deletion worker exists here.
"""
from dataclasses import dataclass, field
from uuid import UUID

from .subject_identity import valid_subject

from .guarded_deletion import VaultSnapshot


@dataclass(frozen=True)
class PreparedGuardedDeletion:
    subject: str = field(repr=False)
    request_id: str
    snapshot: VaultSnapshot = field(repr=False)
    encrypted_user_id: bytes = field(repr=False)
    encrypted_capsule_locator: bytes = field(repr=False)
    requested_at: str
    policy_version: str


class GuardedDeletionRegistration:
    def __init__(self, cipher, store, identity_resolver):
        self.cipher, self.store, self.identity_resolver = cipher, store, identity_resolver

    def prepare(self, *, authenticated_subject: str, request_id: str,
                requested_at: str, policy_version: str) -> PreparedGuardedDeletion:
        try:
            if not valid_subject(authenticated_subject) or str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid deletion identity") from None
        resolved = self.identity_resolver(authenticated_subject)
        if not isinstance(resolved, tuple) or len(resolved) != 2:
            if isinstance(resolved, (list, tuple)):
                for value in resolved:
                    if isinstance(value, bytearray):
                        value[:] = b"\x00" * len(value)
            raise ValueError("deletion identity is unavailable")
        snapshot, internal_id = resolved
        try:
            if not isinstance(snapshot, VaultSnapshot) or snapshot.authenticated_subject != authenticated_subject or \
                    not isinstance(internal_id, bytearray) or not internal_id or \
                    not isinstance(snapshot.capsule_locator, str) or not snapshot.capsule_locator:
                raise ValueError("deletion identity is unavailable")
            locator = bytearray(snapshot.capsule_locator.encode("utf-8"))
            # The cipher clears both mutable buffers even on encryption failure.
            encrypted = self.cipher.seal(request_id, internal_id, locator)
            return PreparedGuardedDeletion(authenticated_subject, request_id, snapshot,
                encrypted["internal_user_id"], encrypted["capsule_locator"], requested_at, policy_version)
        finally:
            if isinstance(internal_id, bytearray):
                internal_id[:] = b"\x00" * len(internal_id)

    def commit(self, *, authenticated_subject: str, prepared: PreparedGuardedDeletion) -> None:
        if not isinstance(prepared, PreparedGuardedDeletion) or prepared.subject != authenticated_subject:
            raise ValueError("deletion owner mismatch")
        self.store.lock_and_register(subject=authenticated_subject, request_id=prepared.request_id,
            snapshot=prepared.snapshot, encrypted_user_id=prepared.encrypted_user_id,
            encrypted_capsule_locator=prepared.encrypted_capsule_locator, requested_at=prepared.requested_at,
            policy_version=prepared.policy_version)
