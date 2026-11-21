import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_capsule_crypto.contract import Entry
from foodtech_capsule_crypto.deletion_plan import DeletionTargets
from foodtech_capsule_crypto.plan_worker import CapsulePlanWorker


class Progress:
    auth_table, registry_table, progress_table = 'auth', 'registry', 'progress'
    def __init__(self):
        self.rows = {}
        self.fail_head = False
    def load_checkpoint(self, *, cipher, subject, request_id, domain):
        return self.rows.get((subject, request_id, domain))
    def save_checkpoint(self, *, cipher, subject, request_id, domain, expected_version, state, checkpoint):
        if domain == 'CAPSULE_PLAN' and expected_version == 1 and self.fail_head:
            self.fail_head = False
            raise RuntimeError('head unavailable')
        key = (subject, request_id, domain)
        existing = self.rows.get(key)
        if (existing['version'] if existing else 0) != expected_version:
            raise RuntimeError('version conflict')
        self.rows[key] = {'version': expected_version + 1, 'state': state, 'checkpoint': checkpoint}
        return expected_version + 1


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.recovery = Mock()
        self.recovery.store = SimpleNamespace(auth_table='auth', registry_table='registry', capsule_table='capsule')
        self.recovery.use_locked.side_effect = lambda **kw: kw['action'](bytearray(str(uuid4()).encode()), bytearray(b'A'*43))
        self.inventory = SimpleNamespace(table_name='capsule')
        self.source = SimpleNamespace(name='capsule', table_arn='arn:source')
        self.progress, self.cipher = Progress(), SimpleNamespace(source_table_arn='arn:source')
        self.worker = CapsulePlanWorker(recovery=self.recovery, inventory=self.inventory, source_table=self.source,
            progress_store=self.progress, progress_cipher=self.cipher, tenant_resolver=object(), key_map={},
            approved_purposes={'BILLING'}, approved_tenant_purposes={'credential'})
        self.entries = tuple(Entry('BILLING', f'2026-{month:02}', 'A'*43) for month in range(1, 13))

    def test_saved_pages_are_reused_after_failure_and_result_hides_targets(self):
        self.progress.fail_head = True
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(self.entries)):
            with self.assertRaises(RuntimeError):
                self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
            output = self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
        self.assertEqual(output, {'state': 'PREPARED', 'target_count': 12, 'pages': 1})
        self.assertNotIn('A'*43, repr(output))
        self.assertEqual(len(self.progress.rows), 2)

    def test_shrinking_plan_after_partial_write_is_rejected(self):
        many = self.entries + self.entries
        self.progress.fail_head = True
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(many)):
            with self.assertRaises(RuntimeError):
                self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(self.entries)):
            with self.assertRaises(ValueError):
                self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
        head = self.progress.rows[(self.subject, self.request, 'CAPSULE_PLAN')]
        self.assertEqual(head['state'], 'IN_PROGRESS')

    def test_missing_page_after_prepared_is_not_silently_recreated(self):
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(self.entries)):
            self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
            del self.progress.rows[(self.subject, self.request, 'CAPSULE_PLAN_PAGE_000000000001')]
            with self.assertRaises(ValueError):
                self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)

    def test_prepared_targets_are_checked_before_dispatch_and_retry_is_explicit(self):
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(self.entries)):
            self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
        seen = []
        output = self.worker.use_prepared(authenticated_subject=self.subject, request_id=self.request, action=seen.append)
        self.assertEqual(output, {'state': 'VISITED', 'target_count': 12})
        self.assertEqual(seen, list(self.entries))
        self.assertNotIn('A'*43, repr(output))
        key = (self.subject, self.request, 'CAPSULE_PLAN_PAGE_000000000001')
        self.progress.rows[key]['checkpoint'] = b'corrupt'
        seen.clear()
        with self.assertRaises(ValueError):
            self.worker.use_prepared(authenticated_subject=self.subject, request_id=self.request, action=seen.append)
        self.assertEqual(seen, [])

    def test_later_page_corruption_never_dispatches_earlier_targets(self):
        entries = tuple(Entry('BILLING', f'{year:04d}-{month:02d}', 'A'*43)
                        for year in (2025, 2026) for month in range(1, 13))
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(entries)):
            self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
        page = (self.subject, self.request, 'CAPSULE_PLAN_PAGE_000000000002')
        self.progress.rows[page]['checkpoint'] = b'corrupt'
        seen = []
        with self.assertRaises(ValueError):
            self.worker.use_prepared(authenticated_subject=self.subject, request_id=self.request, action=seen.append)
        self.assertEqual(seen, [])

    def test_empty_plan_dispatch_does_not_invoke_action(self):
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(())):
            self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
        action = Mock()
        result = self.worker.use_prepared(authenticated_subject=self.subject, request_id=self.request, action=action)
        self.assertEqual(result, {'state': 'VISITED', 'target_count': 0})
        action.assert_not_called()

    def test_changed_plan_or_wrong_source_is_rejected(self):
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(self.entries)):
            self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
        with patch('foodtech_capsule_crypto.plan_worker.prepare_from_resolved_inventory', return_value=DeletionTargets(self.entries[:-1])):
            with self.assertRaises(ValueError):
                self.worker.prepare(authenticated_subject=self.subject, request_id=self.request)
        self.source.table_arn = 'arn:other'
        with self.assertRaises(ValueError):
            CapsulePlanWorker(recovery=self.recovery, inventory=self.inventory, source_table=self.source,
                progress_store=self.progress, progress_cipher=self.cipher, tenant_resolver=object(), key_map={},
                approved_purposes={'BILLING'}, approved_tenant_purposes={'credential'})


if __name__ == '__main__':
    unittest.main()
