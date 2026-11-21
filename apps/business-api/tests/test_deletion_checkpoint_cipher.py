import unittest
from types import SimpleNamespace
from uuid import uuid4

from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher


class Providers:
    def create_aws_kms_keyring(self, *, input):
        return object()


class SDK:
    def encrypt(self, *, source, keyring, encryption_context, algorithm):
        self.source = source
        self.context = dict(encryption_context)
        self.algorithm = algorithm
        return b'ciphertext', SimpleNamespace(algorithm=algorithm)

    def decrypt(self, *, source, keyring, encryption_context):
        if source != b'ciphertext' or encryption_context != self.context:
            raise ValueError('authentication failure')
        return self.source, SimpleNamespace(algorithm=self.algorithm, encryption_context=self.context)


class CipherTests(unittest.TestCase):
    def test_bound_round_trip_and_cross_scope_rejection(self):
        sdk = SDK()
        cipher = DeletionCheckpointCipher('arn:aws:kms:ap-southeast-2:123456789012:key/abcd', 'dev',
                                          source_table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/tier-dev',
                                          kms_client=object(), sdk=sdk, providers=Providers())
        scope = dict(subject=str(uuid4()), request_id=str(uuid4()), domain='COOKING', version=1, state='IN_PROGRESS')
        ciphertext = cipher.seal(**scope, checkpoint=b'opaque checkpoint')
        self.assertEqual(cipher.open(**scope, ciphertext=ciphertext), b'opaque checkpoint')
        for changed in ({'subject': str(uuid4())}, {'request_id': str(uuid4())}, {'domain': 'AI'},
                        {'version': 2}, {'state': 'FAILED'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                cipher.open(**{**scope, **changed}, ciphertext=ciphertext)
        other = DeletionCheckpointCipher('arn:aws:kms:ap-southeast-2:123456789012:key/abcd', 'dev',
            source_table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/other-dev',
            kms_client=object(), sdk=sdk, providers=Providers())
        with self.assertRaises(ValueError):
            other.open(**scope, ciphertext=ciphertext)
        with self.assertRaises(ValueError):
            cipher.seal(**scope, checkpoint=b'')


if __name__ == '__main__':
    unittest.main()
