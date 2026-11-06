import unittest
from types import SimpleNamespace

from aws_encryption_sdk.identifiers import Algorithm

from foodtech_credential_registration.storage import CredentialWriter


KEY_ARN = "arn:aws:kms:ap-southeast-2:573693340100:key/test-key"


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
        self.writer.store("key-1", "pseudo-a", "openai", secret,
                          pool_version="2026-11", shard_id="0")
        self.assertEqual(self.source, b"synthetic-test-key")
        self.assertEqual(secret, bytearray(len(secret)))
        self.assertEqual(self.context["tenant_pseudonym"], "pseudo-a")
        self.assertEqual(self.context["purpose"], "EXTERNAL_AI_CREDENTIAL")
        self.assertEqual(self.table.item["ciphertext"], b"encrypted-message")
        self.assertNotIn("synthetic-test-key", str(self.table.item))

    def test_rejects_overlong_secret_and_wipes_input(self):
        secret = bytearray(b"x" * 401)
        with self.assertRaises(ValueError):
            self.writer.store("key-1", "pseudo-a", "openai", secret,
                              pool_version="2026-11", shard_id="0")
        self.assertEqual(secret, bytearray(len(secret)))
        self.assertIsNone(self.table.item)


if __name__ == "__main__":
    unittest.main()
