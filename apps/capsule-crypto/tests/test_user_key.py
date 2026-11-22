import base64
import unittest
from dataclasses import replace

from foodtech_capsule_crypto.contract import Entry
from foodtech_capsule_crypto.user_entry import open_entry, seal_entry
from foodtech_capsule_crypto.user_key import UserKeyGenerator, UserKeyUnwrapper


def b64(value):
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode().rstrip("=")


class KMS:
    def __init__(self, arn):
        self.arn, self.aad = arn, None

    def generate_data_key(self, *, KeyId, KeySpec, EncryptionContext):
        assert KeyId == self.arn and KeySpec == "AES_256"
        self.aad = EncryptionContext.copy()
        return {"KeyId": KeyId, "Plaintext": b"x" * 32, "CiphertextBlob": b"wrapped-user-key"}

    def decrypt(self, *, KeyId, CiphertextBlob, EncryptionContext, EncryptionAlgorithm):
        if CiphertextBlob != b"wrapped-user-key" or EncryptionContext != self.aad:
            raise ValueError("invalid wrapped key")
        assert KeyId == self.arn and EncryptionAlgorithm == "SYMMETRIC_DEFAULT"
        return {"KeyId": KeyId, "Plaintext": b"x" * 32}


class UserKeyTests(unittest.TestCase):
    def setUp(self):
        self.arn = "arn:aws:kms:ap-southeast-2:123456789012:key/test-key"
        self.key_map = {("pool-v1", "shard-0"): self.arn}
        self.kms = KMS(self.arn)
        self.purposes, self.tenant_purposes = frozenset({"BILLING"}), frozenset({"credential"})
        self.tenant = f"psn:v1:credential:2026-11:v1:{b64(1)}"
        self.entry = Entry("BILLING", "2026-11", b64(2))
        self.generator = UserKeyGenerator(self.key_map, self.tenant_purposes, kms_client=self.kms)
        self.unwrapper = UserKeyUnwrapper(self.key_map, self.tenant_purposes, kms_client=self.kms)
        self.buffers = []

    def seal(self, key, record):
        self.buffers.append(key)
        return seal_entry(key=key, record=record, key_map=self.key_map, tenant=self.tenant, entry=self.entry,
                          approved_purposes=self.purposes, approved_tenant_purposes=self.tenant_purposes,
                          kms_client=self.kms)

    def test_user_key_wraps_real_sdk_entry_and_mutable_buffers_are_cleared(self):
        record, encrypted = self.generator.generate(tenant=self.tenant, pool_version="pool-v1", shard_id="shard-0",
                                                     use_key=self.seal)
        self.assertEqual(bytes(self.buffers[0]), bytes(32))
        self.assertNotIn("wrapped-user-key", repr(record))
        self.assertNotIn("ciphertext", repr(encrypted))
        self.assertEqual(self.kms.aad["purpose"], "CAPSULE_USER_KEY")
        def open_with(key, user_record):
            self.buffers.append(key)
            return open_entry(key=key, record=user_record, key_map=self.key_map, tenant=self.tenant,
                envelope=encrypted, expected_purpose="BILLING", expected_epoch="2026-11",
                approved_purposes=self.purposes, approved_tenant_purposes=self.tenant_purposes, kms_client=self.kms)
        self.assertEqual(self.unwrapper.use(tenant=self.tenant, record=record, use_key=open_with), self.entry)
        self.assertEqual(bytes(self.buffers[-1]), bytes(32))
        with self.assertRaises(ValueError):
            self.unwrapper.use(tenant=self.tenant, record=replace(record, encrypted_dek=b"tampered"), use_key=open_with)
        with self.assertRaises(ValueError):
            self.unwrapper.use(tenant=f"psn:v1:credential:2026-11:v1:{b64(3)}", record=record, use_key=open_with)
        with self.assertRaises(ValueError):
            self.unwrapper.use(tenant=self.tenant, record=record,
                use_key=lambda key, user_record: open_entry(key=key, record=user_record, key_map=self.key_map,
                    tenant=self.tenant, envelope=replace(encrypted, user_key_id="8fd23dcf-1c20-47a4-94da-c91479e9aa04"),
                    expected_purpose="BILLING", expected_epoch="2026-11", approved_purposes=self.purposes,
                    approved_tenant_purposes=self.tenant_purposes, kms_client=self.kms))

    def test_callback_failure_clears_key_and_does_not_return_record(self):
        def fail(key, record):
            self.buffers.append(key)
            raise RuntimeError("private error")
        with self.assertRaisesRegex(ValueError, "^unable to create capsule user key$"):
            self.generator.generate(tenant=self.tenant, pool_version="pool-v1", shard_id="shard-0", use_key=fail)
        self.assertEqual(bytes(self.buffers[0]), bytes(32))


if __name__ == "__main__":
    unittest.main()
