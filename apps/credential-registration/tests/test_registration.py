import base64
import hashlib
import hmac
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from botocore.exceptions import ClientError

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
        self.failure = None

    def transact_write_items(self, *, TransactItems):
        self.items = TransactItems
        if self.failure:
            raise self.failure


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
                                kms_client=self.kms), auth_table="auth-test", dynamodb_client=dynamo)
        credential_id = registrar.register("subject-1", "openai", "2026-11",
                                           self.ciphertext(b"synthetic-api-key"), 2)
        self.assertEqual(writer.seen_plaintext, b"synthetic-api-key")
        self.assertEqual(bytes(writer.buffer), b"\x00" * len(writer.buffer))
        self.assertEqual(len(dynamo.items), 3)
        self.assertEqual(dynamo.items[0]["ConditionCheck"]["ExpressionAttributeValues"][":expected"], {"N": "2"})
        self.assertEqual(dynamo.items[0]["ConditionCheck"]["ReturnValuesOnConditionCheckFailure"], "NONE")
        self.assertIn("attribute_not_exists(#epoch)", dynamo.items[0]["ConditionCheck"]["ConditionExpression"])
        self.assertIn("deletion_lock_request", dynamo.items[0]["ConditionCheck"]["ConditionExpression"])
        self.assertEqual([item["Put"]["TableName"] for item in dynamo.items[1:]],
                         ["vault-test", "registry-test"])
        self.assertNotIn("synthetic-api-key", repr(dynamo.items))
        metadata = dynamo.items[2]["Put"]["Item"]
        self.assertEqual(metadata["credential_id"]["S"], credential_id)
        self.assertEqual(metadata["tenant_id"]["S"], "subject-1")
        self.assertTrue(metadata["tenant_pseudonym"]["S"].startswith("psn:v1:credential:"))
        self.assertEqual(metadata["vault_ref"]["S"], f"vault:{credential_id}")

    def test_locked_registration_reports_conflict_and_erases_buffer(self):
        writer, dynamo = FakeWriter(), FakeDynamo()
        dynamo.failure = ClientError({'Error': {'Code': 'TransactionCanceledException'},
                                      'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        registrar = CredentialRegistrar(writer, 'registry-test', self.transfer,
            TenantPseudonymizer('arn:aws:kms:ap-southeast-2:123:key/hmac', 'v1', kms_client=self.kms),
            auth_table='auth-test', dynamodb_client=dynamo)
        with self.assertRaises(RegistrationError) as caught:
            registrar.register('subject-1', 'openai', '2026-11', self.ciphertext(b'synthetic'), 0)
        self.assertEqual(caught.exception.code, 'TENANT_UNAVAILABLE')
        self.assertEqual(bytes(writer.buffer), b'\x00' * len(writer.buffer))

    def test_initialized_registration_requires_existing_principal_and_exact_epoch(self):
        writer, dynamo = FakeWriter(), FakeDynamo()
        with patch.dict('os.environ', {'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true'}):
            registrar = CredentialRegistrar(writer, 'registry-test', self.transfer,
                TenantPseudonymizer('arn:aws:kms:example', 'v1', kms_client=self.kms),
                auth_table='auth-test', dynamodb_client=dynamo)
        registrar.register('subject-1', 'openai', '2026-11', self.ciphertext(b'synthetic'), 0)
        guard = dynamo.items[0]['ConditionCheck']
        self.assertEqual(guard['ReturnValuesOnConditionCheckFailure'], 'NONE')
        self.assertIn('attribute_exists(pk)', guard['ConditionExpression'])
        self.assertNotIn('attribute_not_exists(#epoch)', guard['ConditionExpression'])
        self.assertNotIn(':zero', guard['ExpressionAttributeValues'])
        self.assertEqual(guard['ExpressionAttributeValues'][':expected'], {'N': '0'})
        self.assertEqual(bytes(writer.buffer), b'\x00' * len(writer.buffer))


class PseudonymValidationTests(unittest.TestCase):
    class Kms:
        def __init__(self, mac=b"x" * 32):
            self.calls = []
            self.mac = mac

        def generate_mac(self, **kwargs):
            self.calls.append(kwargs)
            return {"Mac": self.mac}

    def test_legacy_bytes_and_valid_identifier_are_preserved(self):
        kms = self.Kms()
        pseudonym = TenantPseudonymizer("arn:aws:kms:example", "v1", kms_client=kms).create("subject-1", "2026-11")
        fields = ("v1", "credential", "2026-11", "v1", "subject-1")
        expected = b"".join(len(x.encode()).to_bytes(4, "big") + x.encode() for x in fields)
        self.assertEqual(kms.calls[0]["Message"], expected)
        self.assertEqual(pseudonym, "psn:v1:credential:2026-11:v1:" + base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="))

    def test_invalid_scope_fails_before_kms(self):
        kms = self.Kms()
        generator = TenantPseudonymizer("arn:aws:kms:example", "v1", kms_client=kms)
        for subject, epoch in (("x", "0000-01"), ("x", "2026-13"), ("x", "2026-aa"),
                               ("x", "2026-00"), ("", "2026-11"), ("\ud800", "2026-11"),
                               (None, "2026-11"), ("x", None)):
            with self.subTest(epoch=epoch), self.assertRaises(RegistrationError):
                generator.create(subject, epoch)
        self.assertEqual(kms.calls, [])

    def test_key_version_and_mac_must_match_identifier_contract(self):
        for version in ("", "v:1", "v/1", "x" * 65):
            with self.subTest(version=version), self.assertRaises(ValueError):
                TenantPseudonymizer("arn:aws:kms:example", version, kms_client=self.Kms())
        for mac in (b"", b"x" * 31, b"x" * 33, "not-bytes"):
            with self.subTest(mac=mac), self.assertRaises(RegistrationError):
                TenantPseudonymizer("arn:aws:kms:example", "v1", kms_client=self.Kms(mac)).create("x", "2026-11")


if __name__ == "__main__":
    unittest.main()
