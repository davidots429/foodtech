import unittest
from types import SimpleNamespace

from aws_encryption_sdk.identifiers import Algorithm

from foodtech_credential_registration.storage import CredentialWriter
from foodtech_credential_registration.registration import TenantPseudonymizer


KEY_ARN = "arn:aws:kms:ap-southeast-2:573693340100:key/test-key"
PSEUDONYM = "psn:v1:credential:2026-11:v1:" + "A" * 43


class FakeTable:
    def __init__(self):
        self.item = None

    def put_item(self, *, Item, ConditionExpression):
        if self.item:
            raise ValueError("duplicate")
        self.item = Item


class CredentialWriterTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        resource = SimpleNamespace(Table=lambda name: self.table)

        def encrypt(**kwargs):
            self.context = kwargs["encryption_context"]
            self.source = kwargs["source"]
            self.assertEqual(kwargs["algorithm"],
                             Algorithm.AES_256_GCM_HKDF_SHA512_COMMIT_KEY_ECDSA_P384)
            return b"encrypted-message", SimpleNamespace(algorithm=kwargs["algorithm"])

        self.writer = CredentialWriter(
            "vault-test", "dev", KEY_ARN, resource=resource, kms_client=object(),
            sdk_client=SimpleNamespace(encrypt=encrypt),
            material_providers=SimpleNamespace(create_aws_kms_keyring=lambda **_kwargs: object()))

    def test_encrypts_with_bound_context_and_wipes_mutable_input(self):
        secret = bytearray(b"synthetic-test-key")
        self.writer.store("key-1", PSEUDONYM, "openai", secret,
                          pool_version="2026-11", shard_id="0")
        self.assertEqual(self.source, b"synthetic-test-key")
        self.assertEqual(secret, bytearray(len(secret)))
        self.assertEqual(self.context["tenant_pseudonym"], PSEUDONYM)
        self.assertEqual(self.context["purpose"], "EXTERNAL_AI_CREDENTIAL")
        self.assertEqual(self.table.item["ciphertext"], b"encrypted-message")
        self.assertNotIn("synthetic-test-key", str(self.table.item))

    def test_rejects_overlong_secret_and_wipes_input(self):
        secret = bytearray(b"x" * 401)
        with self.assertRaises(ValueError):
            self.writer.store("key-1", PSEUDONYM, "openai", secret,
                              pool_version="2026-11", shard_id="0")
        self.assertEqual(secret, bytearray(len(secret)))
        self.assertIsNone(self.table.item)

    def test_generator_output_is_accepted_by_writer(self):
        kms = SimpleNamespace(generate_mac=lambda **_: {"Mac": b"x" * 32})
        pseudonym = TenantPseudonymizer(KEY_ARN, "v1", kms_client=kms).create("subject-1", "2026-11")
        self.writer.store("key-1", pseudonym, "openai", bytearray(b"synthetic"),
                          pool_version="2026-11", shard_id="0")
        self.assertEqual(self.context["tenant_pseudonym"], pseudonym)

    def test_raw_or_malformed_context_is_rejected_before_encryption_and_wiped(self):
        for value in ("subject-1", "user@example.com", PSEUDONYM[:-1] + "B",
                      PSEUDONYM.replace("2026-11", "2026-13"),
                      PSEUDONYM.replace("credential", "other"), None):
            secret = bytearray(b"synthetic")
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.writer.store("key-1", value, "openai", secret, pool_version="2026-11", shard_id="0")
            self.assertEqual(secret, bytearray(len(secret)))
            self.assertIsNone(self.table.item)
            self.assertFalse(hasattr(self, "context"))


if __name__ == "__main__":
    unittest.main()
