import hmac
import hashlib
import unittest
from uuid import uuid4

from foodtech_capsule_crypto.tenant_scope import TenantScope, TenantScopeResolver


ARN = 'arn:aws:kms:ap-southeast-2:123456789012:key/test-key'


class Kms:
    def generate_mac(self, *, KeyId, Message, MacAlgorithm):
        self.message = Message
        return {'KeyId': KeyId, 'Mac': hmac.new(b'synthetic', Message, hashlib.sha256).digest(),
                'MacAlgorithm': MacAlgorithm}


class ScopeTests(unittest.TestCase):
    def test_stable_length_prefixed_scope_and_purpose_period_isolation(self):
        kms = Kms()
        resolver = TenantScopeResolver(key_map={('credential', 'v1'): ARN},
                                       approved_purposes={'credential'}, kms_client=kms)
        identity = bytearray(str(uuid4()).encode())
        scope = TenantScope('credential', '2026-11', 'v1')
        first = resolver.derive(internal_id=identity, scope=scope)
        expected = b''.join(len(x.encode()).to_bytes(4, 'big') + x.encode()
                            for x in ('v1', 'credential', '2026-11', 'v1', identity.decode()))
        self.assertEqual(kms.message, expected)
        self.assertEqual(first, resolver.derive(internal_id=identity, scope=scope))
        self.assertNotEqual(first, resolver.derive(internal_id=identity, scope=TenantScope('credential', '2026-12', 'v1')))
        self.assertNotIn(identity.decode(), first)

    def test_invalid_scope_and_unapproved_key_fail_closed(self):
        resolver = TenantScopeResolver(key_map={('credential', 'v1'): ARN}, approved_purposes={'credential'}, kms_client=Kms())
        for scope in (TenantScope('unknown', '2026-11', 'v1'), TenantScope('credential', '2026-13', 'v1'),
                      TenantScope('credential', '2026-11', 'v2')):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                resolver.derive(internal_id=bytearray(str(uuid4()).encode()), scope=scope)
        with self.assertRaises(ValueError):
            resolver.derive(internal_id=bytearray(b'not-a-uuid'), scope=TenantScope('credential', '2026-11', 'v1'))


if __name__ == '__main__':
    unittest.main()
