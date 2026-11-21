"""Internal preparation of an owner-bound deletion registration.

Not a user endpoint: authentication, an authoritative Capsule lookup, durable
retry storage and saga execution must be provided before enabling this path.
"""
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class PreparedDeletion:
    subject: str = field(repr=False)
    request_id: str
    encrypted_user_id: bytes = field(repr=False)
    encrypted_capsule_locator: bytes = field(repr=False)
    requested_at: str
    policy_version: str


class DeletionRegistration:
    def __init__(self, cipher, store, capsule_lookup):
        self.cipher, self.store, self.capsule_lookup = cipher, store, capsule_lookup

    def prepare(self, *, authenticated_subject: str, request_id: str,
                requested_at: str, policy_version: str) -> PreparedDeletion:
        """Resolve the Capsule server-side and encrypt the authenticated owner."""
        try:
            if str(UUID(authenticated_subject)) != authenticated_subject or str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid deletion identity") from None
        locator = self.capsule_lookup(authenticated_subject)
        if not isinstance(locator, bytearray) or not locator:
            if isinstance(locator, bytearray):
                locator[:] = b"\x00" * len(locator)
            raise ValueError("deletion capsule is unavailable")
        # The cipher clears both mutable buffers even when encryption fails.
        encrypted = self.cipher.seal(request_id, bytearray(authenticated_subject.encode("utf-8")), locator)
        return PreparedDeletion(subject=authenticated_subject, request_id=request_id,
            encrypted_user_id=encrypted["internal_user_id"], encrypted_capsule_locator=encrypted["capsule_locator"],
            requested_at=requested_at, policy_version=policy_version)

    def commit(self, *, authenticated_subject: str, prepared: PreparedDeletion) -> bool:
        """Apply a prepared request only to the same authenticated owner."""
        if not isinstance(prepared, PreparedDeletion) or prepared.subject != authenticated_subject:
            raise ValueError("deletion owner mismatch")
        return self.store.lock_and_register(subject=authenticated_subject, request_id=prepared.request_id,
            encrypted_user_id=prepared.encrypted_user_id, encrypted_capsule_locator=prepared.encrypted_capsule_locator,
            requested_at=prepared.requested_at, policy_version=prepared.policy_version)
