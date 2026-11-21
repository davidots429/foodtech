import unittest
from concurrent.futures import ThreadPoolExecutor

from foodtech_business_api.auth_store import MemoryStore, Session


class RefreshStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.save_session('id', Session('customer', 'subject', None, 'csrf', 300, 0,
                                              refresh_ciphertext=b'cipher', refresh_deadline=300))

    def test_only_one_concurrent_claim_and_rotation(self):
        leases = [f'lease-{i:020d}' for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(executor.map(lambda lease: self.store.claim_refresh('id', lease, 100, 130), leases))
        winners = [i for i, result in enumerate(claimed) if result]
        self.assertEqual(len(winners), 1)
        winner = leases[winners[0]]
        self.assertTrue(self.store.finish_refresh('id', winner, 0, 110, 'new-access', b'new-cipher', 250))
        self.assertFalse(self.store.finish_refresh('id', winner, 0, 110, 'old-access', b'old-cipher', 250))
        self.assertEqual(self.store.sessions['id'].refresh_version, 1)
        self.assertEqual(self.store.sessions['id'].refresh_ciphertext, b'new-cipher')

    def test_logout_and_expired_lease_cannot_restore_session(self):
        lease = 'lease-1234567890123456'
        self.assertIsNotNone(self.store.claim_refresh('id', lease, 100, 130))
        self.store.delete_session('id')
        self.assertFalse(self.store.finish_refresh('id', lease, 0, 110, 'access', b'cipher', 250))
        self.assertNotIn('id', self.store.sessions)
        self.setUp()
        self.assertIsNotNone(self.store.claim_refresh('id', lease, 100, 130))
        self.assertFalse(self.store.finish_refresh('id', lease, 0, 130, 'access', b'cipher', 250))
        self.assertFalse(self.store.finish_refresh('id', lease, 0, 110, 'access', b'cipher', 301))
        other = 'other-1234567890123456'
        self.assertIsNotNone(self.store.claim_refresh('id', other, 130, 160))
        self.assertFalse(self.store.finish_refresh('id', lease, 0, 131, 'access', b'cipher', 250))

    def test_missing_expired_and_legacy_sessions_cannot_claim(self):
        lease = 'lease-1234567890123456'
        self.assertIsNone(self.store.claim_refresh('missing', lease, 100, 130))
        self.assertIsNone(self.store.claim_refresh('id', lease, 300, 330))
        self.store.sessions['id'].refresh_ciphertext = None
        self.assertIsNone(self.store.claim_refresh('id', lease, 100, 130))
        with self.assertRaises(ValueError): self.store.claim_refresh('id', lease, 100, 161)
