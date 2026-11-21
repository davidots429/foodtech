import unittest
from types import SimpleNamespace
from uuid import uuid4

from foodtech_business_api.deletion_registry_cipher import DeletionRegistryCipher, SUITE


class FakeProviders:
    def create_aws_kms_keyring(self, *, input):
        return object()


class FakeSDK:
    def __init__(self):
        self.calls = []
        self.fail_at = None

    def encrypt(self, *, source, keyring, encryption_context, algorithm):
        self.calls.append((source, encryption_context.copy(), algorithm))
        if len(self.calls) == self.fail_at:
            raise RuntimeError("sensitive provider error")
        return (b"ciphertext-" + str(len(self.calls)).encode(), SimpleNamespace(algorithm=algorithm))


class DeletionRegistryCipherTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.cipher = DeletionRegistryCipher("arn:aws:kms:ap-southeast-2:123456789012:key/abcd-1234", "dev",
                                             kms_client=object(), sdk=self.sdk, providers=FakeProviders())
        self.request_id = str(uuid4())

    def test_ciphertexts_are_field_and_request_bound_and_inputs_are_wiped(self):
        user, locator = bytearray(b"private-user"), bytearray(b"private-capsule")
        result = self.cipher.seal(self.request_id, user, locator)
        self.assertEqual(result, {"internal_user_id": b"ciphertext-1", "capsule_locator": b"ciphertext-2"})
        self.assertEqual(user, bytes(len(user)))
        self.assertEqual(locator, bytes(len(locator)))
        contexts = [call[1] for call in self.sdk.calls]
        self.assertEqual([item["field"] for item in contexts], ["internal_user_id", "capsule_locator"])
        self.assertTrue(all(item["request_id"] == self.request_id and item["purpose"] == "DELETION_REGISTRY"
                            and item["environment"] == "dev" for item in contexts))
        self.assertTrue(all(call[2] == SUITE for call in self.sdk.calls))
        self.assertFalse(hasattr(self.cipher, "open"))

    def test_second_encryption_failure_erases_both_inputs_and_hides_provider_error(self):
        self.sdk.fail_at = 2
        user, locator = bytearray(b"private-user"), bytearray(b"private-capsule")
        with self.assertRaisesRegex(ValueError, "^unable to encrypt deletion registry locators$"):
            self.cipher.seal(self.request_id, user, locator)
        self.assertEqual(user, bytes(len(user)))
        self.assertEqual(locator, bytes(len(locator)))

    def test_invalid_input_and_aliased_buffers_are_erased(self):
        for request_id in ("bad-id", self.request_id.upper()):
            user, locator = bytearray(b"user"), bytearray(b"locator")
            with self.assertRaises(ValueError):
                self.cipher.seal(request_id, user, locator)
            self.assertEqual(user, bytes(len(user)))
            self.assertEqual(locator, bytes(len(locator)))
        same = bytearray(b"user")
        with self.assertRaises(ValueError):
            self.cipher.seal(self.request_id, same, same)
        self.assertEqual(same, bytes(len(same)))

    def test_wrong_key_region_is_rejected(self):
        with self.assertRaises(ValueError):
            DeletionRegistryCipher("arn:aws:kms:us-east-1:123456789012:key/abcd", "dev", kms_client=object(),
                                   sdk=self.sdk, providers=FakeProviders())


if __name__ == "__main__":
    unittest.main()
