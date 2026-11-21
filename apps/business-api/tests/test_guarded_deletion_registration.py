import unittest
from dataclasses import replace
from uuid import uuid4

from foodtech_business_api.guarded_deletion import VaultSnapshot
from foodtech_business_api.guarded_deletion_registration import GuardedDeletionRegistration


class Cipher:
    def __init__(self):
        self.inputs = []

    def seal(self, request_id, internal_id, locator):
        self.inputs.append((request_id, bytes(internal_id), bytes(locator)))
        internal_id[:] = b"\0" * len(internal_id)
        locator[:] = b"\0" * len(locator)
        return {"internal_user_id": b"encrypted-id", "capsule_locator": b"encrypted-locator"}


class Store:
    def __init__(self):
        self.calls = []

    def lock_and_register(self, **kwargs):
        self.calls.append(kwargs)


class GuardedDeletionRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request_id = str(uuid4()), str(uuid4())
        self.snapshot = VaultSnapshot(self.subject, str(uuid4()), 4, "A" * 43, 8, 0)
        self.internal_id = bytearray(b"private-internal-id")
        self.cipher, self.store = Cipher(), Store()
        self.resolved = []
        def resolver(subject):
            self.resolved.append(subject)
            return self.snapshot, self.internal_id
        self.registration = GuardedDeletionRegistration(self.cipher, self.store, resolver)

    def test_same_server_resolved_owner_and_capsule_are_encrypted_and_guarded(self):
        prepared = self.registration.prepare(authenticated_subject=self.subject, request_id=self.request_id,
                                               requested_at="2026-11-21T00:00:00Z", policy_version="v1")
        self.assertEqual(self.resolved, [self.subject])
        self.assertEqual(self.cipher.inputs, [(self.request_id, b"private-internal-id", b"A" * 43)])
        self.assertEqual(self.internal_id, bytes(len(self.internal_id)))
        self.assertNotIn(self.subject, repr(prepared))
        self.assertNotIn("A" * 43, repr(prepared))
        self.registration.commit(authenticated_subject=self.subject, prepared=prepared)
        self.assertEqual(self.store.calls[0]["snapshot"], self.snapshot)
        self.assertEqual(self.store.calls[0]["encrypted_user_id"], b"encrypted-id")
        with self.assertRaisesRegex(ValueError, "owner mismatch"):
            self.registration.commit(authenticated_subject=str(uuid4()), prepared=prepared)
        self.assertEqual(len(self.store.calls), 1)

    def test_wrong_resolved_owner_wipes_identity_before_rejecting(self):
        other = replace(self.snapshot, authenticated_subject=str(uuid4()))
        self.registration.identity_resolver = lambda _: (other, self.internal_id)
        with self.assertRaisesRegex(ValueError, "identity is unavailable"):
            self.registration.prepare(authenticated_subject=self.subject, request_id=self.request_id,
                                      requested_at="2026-11-21T00:00:00Z", policy_version="v1")
        self.assertEqual(self.internal_id, bytes(len(self.internal_id)))
        self.assertEqual(self.store.calls, [])


if __name__ == "__main__":
    unittest.main()
