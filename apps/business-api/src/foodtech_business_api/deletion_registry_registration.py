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
    def __init__(self, cipher, store, identity_lookup):
        self.cipher, self.store, self.identity_lookup = cipher, store, identity_lookup

    def prepare(self, *, authenticated_subject: str, request_id: str,
                requested_at: str, policy_version: str) -> PreparedDeletion:
        """Resolve internal ID and Capsule from the authenticated owner server-side."""
        try:
            if str(UUID(authenticated_subject)) != authenticated_subject or str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid deletion identity") from None
        resolved = self.identity_lookup(authenticated_subject)
        if not isinstance(resolved, tuple) or len(resolved) != 2:
            if isinstance(resolved, (tuple, list)):
                for value in resolved:
                    if isinstance(value, bytearray):
                        value[:] = b"\x00" * len(value)
            raise ValueError("deletion capsule is unavailable")
        internal_id, locator = resolved
        if not isinstance(internal_id, bytearray) or not internal_id or not isinstance(locator, bytearray) or not locator:
            for value in resolved:
                if isinstance(value, bytearray):
                    value[:] = b"\x00" * len(value)
            raise ValueError("deletion capsule is unavailable")
        # Cognito subject and the stable internal identity are distinct concepts.
        # The cipher clears both mutable buffers even when encryption fails.
        encrypted = self.cipher.seal(request_id, internal_id, locator)
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

    def recover(self, *, authenticated_subject: str) -> dict | None:
        """Recover nonsecret status after a lost response; never start a new saga."""
        return self.store.locked_request(authenticated_subject)
