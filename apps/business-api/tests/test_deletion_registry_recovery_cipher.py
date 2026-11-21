import unittest
from types import SimpleNamespace
from uuid import uuid4

from foodtech_business_api.deletion_registry_cipher import SUITE
from foodtech_business_api.deletion_registry_recovery_cipher import DeletionRegistryRecoveryCipher


class Providers:
    def create_aws_kms_keyring(self, *, input): return object()


class SDK:
    def __init__(self, request):
        self.request = request
        self.values = {b'user': str(uuid4()).encode(), b'locator': b'A' * 43}
    def decrypt(self, *, source, keyring, encryption_context):
        if encryption_context['request_id'] != self.request or \
                (source == b'user') != (encryption_context['field'] == 'internal_user_id'):
            raise ValueError('scope mismatch')
        return self.values[source], SimpleNamespace(algorithm=SUITE, encryption_context=encryption_context)


class RecoveryCipherTests(unittest.TestCase):
    def setUp(self):
        self.request = str(uuid4())
        self.sdk = SDK(self.request)
        self.cipher = DeletionRegistryRecoveryCipher('arn:aws:kms:ap-southeast-2:123456789012:key/abcd', 'dev',
                                                      kms_client=object(), sdk=self.sdk, providers=Providers())
        self.args = {'request_id': self.request, 'encrypted_user_id': b'user', 'encrypted_capsule_locator': b'locator'}

    def test_uses_validated_buffers_and_erases_after_callback(self):
        captured = []
        def callback(user, locator):
            captured.extend([user, locator])
            self.assertEqual(bytes(user), self.sdk.values[b'user'])
            self.assertEqual(bytes(locator), b'A' * 43)
            return 'ok'
        self.assertEqual(self.cipher.use(**self.args, callback=callback), 'ok')
        self.assertTrue(all(not any(value) for value in captured))

    def test_rejects_wrong_request_swapped_fields_and_callback_failure(self):
        for changes in ({'request_id': str(uuid4())}, {'encrypted_user_id': b'locator'},
                        {'encrypted_capsule_locator': b'user'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.cipher.use(**{**self.args, **changes}, callback=lambda *_: None)
        captured = []
        def fail(user, locator):
            captured.extend([user, locator])
            raise RuntimeError('secret internal detail')
        with self.assertRaisesRegex(ValueError, '^unable to recover deletion registry locators$'):
            self.cipher.use(**self.args, callback=fail)
        self.assertTrue(all(not any(value) for value in captured))


if __name__ == '__main__':
    unittest.main()
