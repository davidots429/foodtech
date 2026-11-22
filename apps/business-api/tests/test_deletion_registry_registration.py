import unittest
from uuid import uuid4

from foodtech_business_api.deletion_registry_registration import DeletionRegistration


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
        return len(self.calls) == 1


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.subject = str(uuid4())
        self.request_id = str(uuid4())
        self.internal_id = bytearray(b"private-internal-identifier")
        self.locator = bytearray(b"private-capsule")
        self.lookups = []
        self.cipher, self.store = Cipher(), Store()
        def lookup(subject):
            self.lookups.append(subject)
            return self.internal_id, self.locator
        self.registration = DeletionRegistration(self.cipher, self.store, lookup)

    def test_server_lookup_and_authenticated_subject_are_bound_to_registration(self):
        prepared = self.registration.prepare(authenticated_subject=self.subject, request_id=self.request_id,
                                               requested_at="2026-11-21T00:00:00Z", policy_version="v1")
        self.assertEqual(self.lookups, [self.subject])
        self.assertEqual(self.cipher.inputs, [(self.request_id, b"private-internal-identifier", b"private-capsule")])
        self.assertEqual(self.internal_id, bytes(len(self.internal_id)))
        self.assertEqual(self.locator, bytes(len(self.locator)))
        self.assertNotIn(self.subject, repr(prepared))
        self.assertNotIn("encrypted-id", repr(prepared))
        self.assertTrue(self.registration.commit(authenticated_subject=self.subject, prepared=prepared))
        self.assertFalse(self.registration.commit(authenticated_subject=self.subject, prepared=prepared))
        self.assertEqual(self.store.calls[0], self.store.calls[1])
        with self.assertRaisesRegex(ValueError, "owner mismatch"):
            self.registration.commit(authenticated_subject=str(uuid4()), prepared=prepared)
        self.assertEqual(len(self.store.calls), 2)

    def test_missing_capsule_or_invalid_identity_never_registers(self):
        self.registration.identity_lookup = lambda _: None
        with self.assertRaisesRegex(ValueError, "capsule is unavailable"):
            self.registration.prepare(authenticated_subject=self.subject, request_id=self.request_id,
                                      requested_at="2026-11-21T00:00:00Z", policy_version="v1")
        with self.assertRaisesRegex(ValueError, "invalid deletion identity"):
            self.registration.prepare(authenticated_subject="bad", request_id=self.request_id,
                                      requested_at="2026-11-21T00:00:00Z", policy_version="v1")
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.cipher.inputs, [])

    def test_incomplete_identity_lookup_wipes_any_mutable_private_value(self):
        private_id = bytearray(b"private-id")
        self.registration.identity_lookup = lambda _: (private_id, None)
        with self.assertRaisesRegex(ValueError, "capsule is unavailable"):
            self.registration.prepare(authenticated_subject=self.subject, request_id=self.request_id,
                                      requested_at="2026-11-21T00:00:00Z", policy_version="v1")
        self.assertEqual(private_id, bytes(len(private_id)))
        self.assertEqual(self.store.calls, [])


if __name__ == "__main__":
    unittest.main()
