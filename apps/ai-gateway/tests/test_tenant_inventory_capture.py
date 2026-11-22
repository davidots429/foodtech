import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_ai_gateway.legacy_tenant_inventory import InventoryPage
from foodtech_ai_gateway.tenant_inventory_capture import TenantInventoryCapture


class Progress:
    auth_table = 'auth'
    registry_table = 'deletion-registry'
    progress_table = 'progress'
    client = object()
    def __init__(self):
        self.items = {}
        self.fail_head = False
    def load_checkpoint(self, *, cipher, subject, request_id, domain):
        return self.items.get((subject, request_id, domain))
    def save_checkpoint(self, *, cipher, subject, request_id, domain, expected_version, state, checkpoint):
        if domain == 'AI_INVENTORY' and self.fail_head:
            self.fail_head = False
            raise RuntimeError('head write failed')
        key = (subject, request_id, domain)
        item = self.items.get(key)
        if (item['version'] if item else 0) != expected_version:
            raise RuntimeError('version conflict')
        self.items[key] = {'version': expected_version + 1, 'state': state, 'checkpoint': checkpoint}
        return expected_version + 1


class InventoryCaptureTests(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(name='registry', table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/registry')
        self.progress = Progress()
        self.cipher = SimpleNamespace(source_table_arn=self.source.table_arn)
        self.capture = TenantInventoryCapture(source_table=self.source, progress_store=self.progress, cipher=self.cipher)
        self.scope = {'subject': str(uuid4()), 'request_id': str(uuid4())}

    def test_source_cannot_be_deletion_registry(self):
        self.source.name='deletion-registry'
        with self.assertRaises(ValueError):self.capture.capture_page(**self.scope,limit=1)
        self.assertFalse(self.progress.items)

    def test_captures_resumable_pages_and_checks_all_before_exposing(self):
        first = InventoryPage(('EXEC#first',), {'pk': 'other', 'sk': 'key'}, 2)
        second = InventoryPage(('CONSENT#second',), None, 1)
        self.progress.fail_head = True
        with patch('foodtech_ai_gateway.tenant_inventory_capture.reconcile_reserved_page', side_effect=[first, second]) as scan:
            with self.assertRaises(RuntimeError):
                self.capture.capture_page(**self.scope, limit=2)
            self.assertFalse(self.capture.capture_page(**self.scope, limit=2)['complete'])
            self.assertEqual(scan.call_count, 1)
            result = self.capture.capture_page(**self.scope, limit=2)
            self.assertEqual(result, {'complete': True, 'pages': 2, 'count': 2})
        targets = list(self.capture.partitions(**self.scope))
        self.assertEqual([item.partition for item in targets], ['EXEC#first', 'CONSENT#second'])
        self.assertNotIn('EXEC#first', repr(targets))
        del self.progress.items[(self.scope['subject'], self.scope['request_id'], 'AI_INVENTORY_PAGE_000000000002')]
        iterator = self.capture.partitions(**self.scope)
        with self.assertRaises(ValueError):
            next(iterator)

    def test_changed_page_is_rejected_before_its_target_is_exposed(self):
        with patch('foodtech_ai_gateway.tenant_inventory_capture.reconcile_reserved_page',
                   return_value=InventoryPage(('EXEC#first',), None, 1)):
            self.capture.capture_page(**self.scope, limit=1)
        original = self.progress.load_checkpoint
        page_reads = 0
        def changed(**kwargs):
            nonlocal page_reads
            item = original(**kwargs)
            if kwargs['domain'] == 'AI_INVENTORY_PAGE_000000000001':
                page_reads += 1
                if page_reads == 2:
                    return {**item, 'checkpoint': self.capture._pack(
                        {'schema': 1, 'start': None, 'next': None, 'partitions': ['EXEC#other']})}
            return item
        self.progress.load_checkpoint = changed
        with self.assertRaises(ValueError):
            next(self.capture.partitions(**self.scope))

    def test_duplicate_partition_across_pages_is_rejected_before_exposure(self):
        with patch('foodtech_ai_gateway.tenant_inventory_capture.reconcile_reserved_page',
                   side_effect=[InventoryPage(('EXEC#same',), {'pk': 'p', 'sk': 's'}, 1),
                                InventoryPage(('EXEC#same',), None, 1)]):
            self.capture.capture_page(**self.scope, limit=1)
            self.capture.capture_page(**self.scope, limit=1)
        with self.assertRaisesRegex(ValueError, 'duplicate AI inventory partition'):
            next(self.capture.partitions(**self.scope))

    def test_completed_chain_with_cursor_cycle_exposes_no_partitions(self):
        a, b = {'pk': 'a', 'sk': 's'}, {'pk': 'b', 'sk': 's'}
        pages = [InventoryPage(('EXEC#first',), a, 1), InventoryPage((), b, 1),
                 InventoryPage((), a, 1), InventoryPage((), None, 1)]
        with patch('foodtech_ai_gateway.tenant_inventory_capture.reconcile_reserved_page', side_effect=pages):
            for _ in pages:
                self.capture.capture_page(**self.scope, limit=1)
        with self.assertRaisesRegex(ValueError, 'repeated AI inventory cursor'):
            next(self.capture.partitions(**self.scope))

    def test_recurring_cursor_cycle_stops_during_capture(self):
        a, b = {'pk': 'a', 'sk': 's'}, {'pk': 'b', 'sk': 's'}
        pages = [InventoryPage((), cursor, 1) for cursor in (a, b, a, b)]
        with patch('foodtech_ai_gateway.tenant_inventory_capture.reconcile_reserved_page', side_effect=pages):
            for _ in range(3):
                self.capture.capture_page(**self.scope, limit=1)
            with self.assertRaisesRegex(ValueError, 'repeated AI inventory cursor'):
                self.capture.capture_page(**self.scope, limit=1)
        head = self.progress.items[(self.scope['subject'], self.scope['request_id'], 'AI_INVENTORY')]
        self.assertEqual(head['version'], 3)

    def test_legacy_incomplete_inventory_resumes_with_cycle_state(self):
        cursor = {'pk': 'a', 'sk': 's'}
        self.progress.items[(self.scope['subject'], self.scope['request_id'], 'AI_INVENTORY')] = {
            'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': self.capture._pack(
                {'schema': 1, 'pages': 1, 'count': 0, 'cursor': cursor, 'complete': False})}
        self.progress.items[(self.scope['subject'], self.scope['request_id'], 'AI_INVENTORY_PAGE_000000000001')] = {
            'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': self.capture._pack(
                {'schema': 1, 'start': None, 'next': cursor, 'partitions': []})}
        with patch('foodtech_ai_gateway.tenant_inventory_capture.reconcile_reserved_page',
                   return_value=InventoryPage(('EXEC#next',), None, 1)):
            self.assertTrue(self.capture.capture_page(**self.scope, limit=1)['complete'])
        self.assertEqual([item.partition for item in self.capture.partitions(**self.scope)], ['EXEC#next'])

    def test_legacy_saved_page_after_head_failure_can_resume(self):
        cursor = {'pk': 'a', 'sk': 's'}
        self.progress.items[(self.scope['subject'], self.scope['request_id'], 'AI_INVENTORY_PAGE_000000000001')] = {
            'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': self.capture._pack(
                {'schema': 1, 'start': None, 'next': cursor, 'partitions': []})}
        self.assertFalse(self.capture.capture_page(**self.scope, limit=1)['complete'])
        with patch('foodtech_ai_gateway.tenant_inventory_capture.reconcile_reserved_page',
                   return_value=InventoryPage((), None, 1)):
            self.assertTrue(self.capture.capture_page(**self.scope, limit=1)['complete'])

    def test_rejects_other_source_and_nonadvancing_scan(self):
        self.cipher.source_table_arn = 'other'
        with self.assertRaises(ValueError):
            self.capture.capture_page(**self.scope, limit=1)
        self.cipher.source_table_arn = self.source.table_arn
        self.progress.items[(self.scope['subject'], self.scope['request_id'], 'AI_INVENTORY')] = {
            'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': self.capture._pack(
                {'schema': 1, 'pages': 1, 'count': 0, 'cursor': {'pk':'p','sk':'s'}, 'complete': False})}
        with patch('foodtech_ai_gateway.tenant_inventory_capture.reconcile_reserved_page',
                   return_value=InventoryPage((), {'pk':'p','sk':'s'}, 0)):
            with self.assertRaises(ValueError):
                self.capture.capture_page(**self.scope, limit=1)


if __name__ == '__main__':
    unittest.main()
