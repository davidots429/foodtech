import base64
import hashlib
import hmac
import unittest
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from foodtech_credential_registration.registration import (
    CredentialRegistrar, RegistrationError, TenantPseudonymizer, TransferKey,
)


class FakeKms:
    def __init__(self):
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=4096)

    def get_public_key(self, **_kwargs):
        return {"KeySpec": "RSA_4096", "KeyUsage": "ENCRYPT_DECRYPT",
                "EncryptionAlgorithms": ["RSAES_OAEP_SHA_256"],
                "PublicKey": self.private.public_key().public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)}

    def decrypt(self, *, CiphertextBlob, EncryptionAlgorithm, **_kwargs):
        assert EncryptionAlgorithm == "RSAES_OAEP_SHA_256"
        return {"Plaintext": self.private.decrypt(CiphertextBlob, padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))}

    def generate_mac(self, *, Message, **_kwargs):
        return {"Mac": hmac.new(b"synthetic-hmac-key", Message, hashlib.sha256).digest()}


class FakeWriter:
    table = SimpleNamespace(name="vault-test")

    def __init__(self):
        self.seen_plaintext = None
        self.buffer = None

    def encrypt_item(self, credential_id, pseudonym, provider, plaintext, **_kwargs):
        self.seen_plaintext = bytes(plaintext)
        self.buffer = plaintext
        plaintext[:] = b"\x00" * len(plaintext)
        return {"pk": f"CREDENTIAL#{credential_id}", "sk": "META", "provider": provider,
                "tenant_pseudonym": pseudonym, "ciphertext": b"synthetic-ciphertext"}


class FakeDynamo:
    def __init__(self):
        self.items = []

    def transact_write_items(self, *, TransactItems):
        self.items = TransactItems


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.kms = FakeKms()
        self.transfer = TransferKey("arn:aws:kms:ap-southeast-2:123:key/transfer", "2026-11",
                                    kms_client=self.kms)

    def ciphertext(self, value: bytes) -> str:
        descriptor = self.transfer.public_descriptor()
        self.assertEqual(descriptor["algorithm"], "RSA-OAEP-SHA-256")
        return base64.b64encode(self.kms.private.public_key().encrypt(value, padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(),
            label=None))).decode()

    def test_rsa_oaep_round_trip_and_byte_limit(self):
        ciphertext = self.ciphertext("가".encode() * 133)
        self.assertEqual(len(self.transfer.decrypt("2026-11", ciphertext)), 399)
        with self.assertRaises(RegistrationError) as caught:
            self.transfer.decrypt("2026-11", self.ciphertext("가".encode() * 134))
        self.assertEqual(caught.exception.code, "CREDENTIAL_LENGTH_INVALID")
        with self.assertRaises(RegistrationError):
            self.transfer.decrypt("old", ciphertext)
        with self.assertRaises(RegistrationError):
            self.transfer.decrypt("2026-11", "not-base64")

    def test_encrypted_vault_and_metadata_are_written_atomically(self):
        writer, dynamo = FakeWriter(), FakeDynamo()
        registrar = CredentialRegistrar(
            writer, "registry-test", self.transfer,
            TenantPseudonymizer("arn:aws:kms:ap-southeast-2:123:key/hmac", "v1",
                                kms_client=self.kms), dynamodb_client=dynamo)
        credential_id = registrar.register("subject-1", "openai", "2026-11",
                                           self.ciphertext(b"synthetic-api-key"))
        self.assertEqual(writer.seen_plaintext, b"synthetic-api-key")
        self.assertEqual(bytes(writer.buffer), b"\x00" * len(writer.buffer))
        self.assertEqual(len(dynamo.items), 2)
        self.assertEqual([item["Put"]["TableName"] for item in dynamo.items],
                         ["vault-test", "registry-test"])
        self.assertNotIn("synthetic-api-key", repr(dynamo.items))
        metadata = dynamo.items[1]["Put"]["Item"]
        self.assertEqual(metadata["credential_id"]["S"], credential_id)
        self.assertEqual(metadata["tenant_id"]["S"], "subject-1")
        self.assertTrue(metadata["tenant_pseudonym"]["S"].startswith("psn:v1:credential:"))
        self.assertEqual(metadata["vault_ref"]["S"], f"vault:{credential_id}")


if __name__ == "__main__":
    unittest.main()
