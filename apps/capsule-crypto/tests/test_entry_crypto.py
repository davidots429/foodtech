import base64
import unittest
from dataclasses import replace
from types import SimpleNamespace

from foodtech_capsule_crypto.contract import Entry
from foodtech_capsule_crypto.reader import CapsuleEntryReader
from foodtech_capsule_crypto.writer import CapsuleEntryWriter, SUITE


def digest(value):
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode().rstrip("=")


class Providers:
    def create_aws_kms_keyring(self, *, input):
        return object()


class SDK:
    def __init__(self):
        self.records = {}
        self.header_override = None

    def encrypt(self, *, source, keyring, encryption_context, algorithm):
        ciphertext = b"opaque-" + str(len(self.records)).encode()
        self.records[ciphertext] = (source, encryption_context.copy())
        return ciphertext, SimpleNamespace(algorithm=algorithm, encryption_context=encryption_context.copy())

    def decrypt(self, *, source, keyring, encryption_context):
        raw, expected = self.records[source]
        if expected != encryption_context:
            raise ValueError("context mismatch")
        return raw, self.header_override or SimpleNamespace(algorithm=SUITE, encryption_context=expected)


class CapsuleEntryCryptoTests(unittest.TestCase):
    def setUp(self):
        self.sdk, self.providers = SDK(), Providers()
        self.key = "arn:aws:kms:ap-southeast-2:123456789012:key/test-key"
        self.key_map = {("2026-11-v1", "shard-0"): self.key}
        self.purposes = frozenset({"BILLING", "USAGE"})
        kwargs = {"kms_client": object(), "sdk": self.sdk, "providers": self.providers,
                  "approved_tenant_purposes": frozenset({"credential"}), "keyring_factory": lambda _: object()}
        self.writer = CapsuleEntryWriter(self.purposes, self.key_map, **kwargs)
        self.reader = CapsuleEntryReader(self.purposes, self.key_map, **kwargs)
        self.tenant = f"psn:v1:credential:2026-11:v1:{digest(1)}"
        self.entry = Entry("BILLING", "2026-11", digest(2))

    def seal(self):
        return self.writer.seal(tenant_pseudonym=self.tenant, entry=self.entry,
                                pool_version="2026-11-v1", shard_id="shard-0")

    def test_round_trip_uses_scoped_context_and_hides_ciphertext_in_repr(self):
        envelope = self.seal()
        self.assertEqual(self.reader.open(tenant_pseudonym=self.tenant, envelope=envelope,
                                          expected_purpose="BILLING", expected_epoch="2026-11"), self.entry)
        _, aad = self.sdk.records[envelope.ciphertext]
        self.assertEqual(aad, {"tenant_pseudonym": self.tenant, "purpose": "CAPSULE_ENTRY",
                               "pool_version": "2026-11-v1", "shard_id": "shard-0"})
        self.assertNotIn("opaque", repr(envelope))
        self.assertNotIn(self.entry.pseudonym_digest, repr(self.entry))
        self.assertFalse(hasattr(self.writer, "open"))

    def test_other_tenant_epoch_or_key_metadata_is_rejected(self):
        envelope = self.seal()
        cases = [(f"psn:v1:credential:2026-11:v1:{digest(3)}", envelope, "BILLING", "2026-11"),
                 (self.tenant, envelope, "USAGE", "2026-11"), (self.tenant, envelope, "BILLING", "2026-12"),
                 (self.tenant, replace(envelope, shard_id="other"), "BILLING", "2026-11"),
                 (self.tenant, replace(envelope, key_arn=self.key + "x"), "BILLING", "2026-11"),
                 (self.tenant, replace(envelope, ciphertext=b"altered"), "BILLING", "2026-11")]
        for tenant, item, purpose, epoch in cases:
            with self.subTest(purpose=purpose, epoch=epoch), self.assertRaises(ValueError):
                self.reader.open(tenant_pseudonym=tenant, envelope=item, expected_purpose=purpose, expected_epoch=epoch)

    def test_header_mismatch_and_unapproved_input_fail_closed(self):
        envelope = self.seal()
        self.sdk.header_override = SimpleNamespace(algorithm=SUITE, encryption_context={"tenant_pseudonym": "different"})
        with self.assertRaises(ValueError):
            self.reader.open(tenant_pseudonym=self.tenant, envelope=envelope,
                             expected_purpose="BILLING", expected_epoch="2026-11")
        _, aad = self.sdk.records[envelope.ciphertext]
        self.sdk.header_override = SimpleNamespace(algorithm=SUITE, encryption_context={**aad, "extra": "not-allowed"})
        with self.assertRaises(ValueError):
            self.reader.open(tenant_pseudonym=self.tenant, envelope=envelope,
                             expected_purpose="BILLING", expected_epoch="2026-11")
        for entry in (Entry("OTHER", "2026-11", digest(2)), Entry("BILLING", "2026-13", digest(2)),
                      Entry("BILLING", "2026-11", "not-a-digest")):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.writer.seal(tenant_pseudonym=self.tenant, entry=entry, pool_version="2026-11-v1", shard_id="shard-0")
        with self.assertRaises(ValueError):
            self.writer.seal(tenant_pseudonym="private-email@example.com", entry=self.entry,
                             pool_version="2026-11-v1", shard_id="shard-0")
        with self.assertRaises(ValueError):
            self.writer.seal(tenant_pseudonym=f"psn:v1:unknown:2026-11:v1:{digest(1)}", entry=self.entry,
                             pool_version="2026-11-v1", shard_id="shard-0")

    def test_direct_kms_entry_path_is_not_enabled(self):
        with self.assertRaises(ValueError):
            CapsuleEntryWriter(self.purposes, self.key_map, approved_tenant_purposes=frozenset({"credential"}),
                               kms_client=object(), sdk=self.sdk, providers=self.providers)
        with self.assertRaises(ValueError):
            CapsuleEntryReader(self.purposes, self.key_map, approved_tenant_purposes=frozenset({"credential"}),
                               kms_client=object(), sdk=self.sdk, providers=self.providers)


if __name__ == "__main__":
    unittest.main()
