import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_business_api.legacy_profile_scan import LegacyProfilePage
from foodtech_business_api.profile_deletion_inventory import ProfileDeletionInventory
from foodtech_business_api.profile_deletion_worker import ProfileDeletionWorker


class Progress:
    auth_table, registry_table, progress_table = 'auth', 'registry', 'progress'
    client = object()
    def __init__(self): self.items = {}; self.fail_head = False
    def load_checkpoint(self, *, cipher, subject, request_id, domain):
        return self.items.get((subject, request_id, domain))
    def save_checkpoint(self, *, cipher, subject, request_id, domain, expected_version, state, checkpoint):
        if self.fail_head and domain == 'PROFILE_INVENTORY':
            self.fail_head = False
            raise RuntimeError('head unavailable')
        key = (subject, request_id, domain)
        old = self.items.get(key)
        if (old['version'] if old else 0) != expected_version:
            raise RuntimeError('version conflict')
        self.items[key] = {'version': expected_version + 1, 'state': state, 'checkpoint': checkpoint}
        return expected_version + 1


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.scope = {'subject': self.subject, 'request_id': self.request}
        self.source = SimpleNamespace(name='cooking', table_arn='arn:source')
        self.progress, self.cipher = Progress(), SimpleNamespace(source_table_arn='arn:source')
        self.inventory = ProfileDeletionInventory(source_table=self.source, progress_store=self.progress, cipher=self.cipher)

    def capture_two(self):
        first = LegacyProfilePage((('p1', 'v1'),), {'pk': 'other', 'sk': 'key'}, 2)
        second = LegacyProfilePage((('p2', 'v2'),), None, 1)
        self.progress.fail_head = True
        with patch('foodtech_business_api.profile_deletion_inventory.scan_legacy_profiles', side_effect=[first, second]) as scan:
            with self.assertRaises(RuntimeError): self.inventory.capture_page(**self.scope, limit=2)
            self.assertFalse(self.inventory.capture_page(**self.scope, limit=2)['complete'])
            self.assertEqual(scan.call_count, 1)
            self.assertTrue(self.inventory.capture_page(**self.scope, limit=2)['complete'])

    def test_pages_resume_and_are_verified_before_targets(self):
        self.capture_two()
        targets = list(self.inventory.targets(**self.scope))
        self.assertEqual([(target.profile_id, target.version) for target in targets], [('p1','v1'), ('p2','v2')])
        self.assertNotIn('p1', repr(targets))
        del self.progress.items[(self.subject, self.request, 'PROFILE_INVENTORY_PAGE_000000000002')]
        with self.assertRaises(ValueError): next(self.inventory.targets(**self.scope))

    def test_delete_retries_only_captured_target_and_does_not_claim_completion(self):
        self.capture_two()
        worker = ProfileDeletionWorker(inventory=self.inventory, progress_store=self.progress,
                                       cipher=self.cipher, source_table=self.source)
        with patch('foodtech_business_api.profile_deletion_worker.delete_profile_pair') as delete:
            self.assertEqual(worker.delete_one(**self.scope, index=0), {'index': 0, 'state': 'VERIFYING'})
            self.assertEqual(worker.delete_one(**self.scope, index=0), {'index': 0, 'state': 'VERIFYING'})
        self.assertEqual(delete.call_count, 2)
        self.assertEqual(delete.call_args.kwargs['profile_id'], 'p1')
        with self.assertRaises(ValueError): worker.delete_one(**self.scope, index=2)


if __name__ == '__main__':
    unittest.main()
