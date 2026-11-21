import unittest
from types import SimpleNamespace

from foodtech_business_api.session_token_cipher import SessionTokenCipher, SUITE


class FakeProviders:
    def create_aws_kms_keyring(self, *, input):
        return object()


class FakeSDK:
    def encrypt(self, *, source, keyring, encryption_context, algorithm):
        self.source = source
        self.context = encryption_context
        return b'encrypted', SimpleNamespace(algorithm=algorithm)

    def decrypt(self, *, source, keyring, encryption_context):
        if encryption_context != self.context:
            raise ValueError('context mismatch')
        return self.source, SimpleNamespace(algorithm=SUITE, encryption_context=self.context)


class SessionTokenCipherTest(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.cipher = SessionTokenCipher('arn:aws:kms:region:account:key/id', 'dev',
                                         kms_client=object(), sdk=self.sdk, providers=FakeProviders())
        self.session = 'session-1234567890'

    def test_round_trip_binds_session_and_surface_and_wipes_input(self):
        token = bytearray(b'sensitive-refresh-token')
        encrypted = self.cipher.seal(token, self.session, 'customer')
        self.assertEqual(token, bytes(len(token)))
        self.assertNotIn(self.session, str(self.sdk.context))
        self.assertEqual(self.cipher.open(encrypted, self.session, 'customer'), b'sensitive-refresh-token')
        for session, surface in [('other-session-12345', 'customer'), (self.session, 'admin')]:
            with self.assertRaises(ValueError):
                self.cipher.open(encrypted, session, surface)

    def test_invalid_input_wipes_buffer(self):
        token = bytearray(b'sensitive')
        with self.assertRaises(ValueError):
            self.cipher.seal(token, self.session, 'invalid')
        self.assertEqual(token, bytes(len(token)))
        with self.assertRaises(ValueError):
            self.cipher.open(b'', self.session, 'customer')

    def test_decrypted_header_must_match_context_and_suite(self):
        token = bytearray(b'secret')
        encrypted = self.cipher.seal(token, self.session, 'customer')
        self.sdk.decrypt = lambda **kwargs: (b'secret', SimpleNamespace(algorithm=object(), encryption_context=self.sdk.context))
        with self.assertRaises(ValueError):
            self.cipher.open(encrypted, self.session, 'customer')
