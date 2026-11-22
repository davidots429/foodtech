import unittest
from types import SimpleNamespace

from foodtech_business_api.profile_checkpoint_cipher import ProfileCheckpointCipher, SUITE


class FakeSdk:
    def encrypt(self, *, source, encryption_context, **kwargs):
        self.source, self.context = source, encryption_context
        return b'ciphertext', SimpleNamespace(algorithm=SUITE)

    def decrypt(self, *, encryption_context, **kwargs):
        if encryption_context != self.context:
            raise ValueError('mismatch')
        return self.source, SimpleNamespace(algorithm=SUITE, encryption_context=self.context)


class CheckpointCipherTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSdk()
        self.cipher = ProfileCheckpointCipher('arn:aws:kms:ap-southeast-2:123456789012:key/test', 'dev',
            kms_client=object(), sdk=self.sdk,
            providers=SimpleNamespace(create_aws_kms_keyring=lambda **_: object()))
        self.job = '12c5f2d4-c6d2-4165-adf7-e7fed537af06'

    def test_checkpoint_roundtrip_is_job_bound(self):
        checkpoint = {'pk': 'private-owner-key', 'sk': 'META'}
        encrypted = self.cipher.seal(self.job, 'owner-a', checkpoint, phase='CONTINUE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking')
        self.assertNotIn(b'private-owner-key', encrypted)
        self.assertEqual(self.cipher.open(self.job, 'owner-a', encrypted, phase='CONTINUE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking'), checkpoint)
        self.assertNotIn('private-owner-key', repr(self.sdk.context))
        with self.assertRaises(ValueError):
            self.cipher.open('e4248c1f-3496-45eb-b828-2177c34eb854', 'owner-a', encrypted, phase='CONTINUE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking')
        with self.assertRaises(ValueError):
            self.cipher.open(self.job, 'owner-b', encrypted, phase='CONTINUE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking')
        with self.assertRaises(ValueError):
            self.cipher.open(self.job, 'owner-a', encrypted, phase='CONTINUE', version=3, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking')
        with self.assertRaises(ValueError):
            self.cipher.open(self.job, 'owner-a', encrypted, phase='SCAN_COMPLETE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking')

    def test_rejects_invalid_checkpoint_and_unexpected_context(self):
        for checkpoint in ({'pk': 'x'}, {'pk': 'x', 'sk': ''}, {'pk': 'x' * 2049, 'sk': 'x'}):
            with self.subTest(checkpoint=tuple(checkpoint)), self.assertRaises(ValueError):
                self.cipher.seal(self.job, 'owner-a', checkpoint, phase='INITIAL', version=1, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking')
        self.cipher.seal(self.job, 'owner-a', {'pk': 'x', 'sk': 'y'}, phase='INITIAL', version=1, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking')
        original = self.sdk.decrypt
        def unexpected(**kwargs):
            raw, header = original(**kwargs)
            return raw, SimpleNamespace(algorithm=SUITE, encryption_context={**header.encryption_context, 'other': 'x'})
        self.sdk.decrypt = unexpected
        with self.assertRaises(ValueError):
            self.cipher.open(self.job, 'owner-a', b'ciphertext', phase='INITIAL', version=1, source_table='arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking')


if __name__ == '__main__':
    unittest.main()
