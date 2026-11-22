import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from foodtech_business_api.withdrawal_identity import IdentityReleaseCursor
from foodtech_business_api.withdrawal_identity_worker import sweep_identities, main, configured_worker


class IdentityWorkerTests(unittest.TestCase):
    def worker(self, pages):
        calls = []
        def scan(**kwargs):
            calls.append(kwargs)
            value = next(pages)
            if isinstance(value, Exception):
                raise value
            return value
        return SimpleNamespace(scan_page=scan, auth_table=SimpleNamespace(name='auth')), calls

    def test_empty_intermediate_page_retains_cursor_and_counts_failures(self):
        cursor = IdentityReleaseCursor('auth', {'pk': 'SESSION#synthetic-secret'})
        worker, calls = self.worker(iter([
            {'results': [], 'next_cursor': cursor},
            {'results': [{'state': state, 'deletion_complete': False} for state in (
                'COGNITO_REMOVED', 'BLOCKED', 'RECONCILIATION_REQUIRED')], 'next_cursor': None}]))
        result = sweep_identities(worker, page_size=3)
        self.assertEqual(result, {'pages': 2, 'identity_removed': 1, 'blocked': 1, 'reconciliation_required': 1,
            'scan_finished': True, 'deletion_complete': False})
        self.assertIs(calls[1]['cursor'], cursor)
        self.assertNotIn('synthetic-secret', repr(result))

    def test_cycle_limit_unknown_state_and_completion_claim_are_failures(self):
        cursor = IdentityReleaseCursor('auth', {'pk': 'SESSION#synthetic-secret'})
        for pages, limit in (([{'results': [], 'next_cursor': cursor}] * 2, 3),
                             ([{'results': [], 'next_cursor': cursor}], 1),
                             ([{'results': [{'state': 'DONE', 'deletion_complete': False}], 'next_cursor': None}], 3),
                             ([{'results': [{'state': 'COGNITO_REMOVED', 'deletion_complete': True}], 'next_cursor': None}], 3)):
            with self.subTest(pages=repr(pages)):
                worker, _ = self.worker(iter(pages))
                with self.assertRaises((ValueError, RuntimeError)):
                    sweep_identities(worker, max_pages=limit)

    def test_missing_opt_in_mismatched_stages_and_invalid_bounds_do_not_create_clients(self):
        variants = ({}, {'FOODTECH_WITHDRAWAL_IDENTITY_WORKER_ENABLED': 'true'},
                    {'FOODTECH_WITHDRAWAL_IDENTITY_PAGE_SIZE': '0'},
                    {'FOODTECH_WITHDRAWAL_IDENTITY_MAX_PAGES': 'not-an-integer'},
                    {'FOODTECH_WITHDRAWAL_IDENTITY_WORKER_ENABLED': 'true',
                     'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev',
                     'FOODTECH_DELETION_REGISTRY_TABLE': 'foodtech-deletion-registry-demo',
                     'FOODTECH_COGNITO_USER_POOL_ID': 'ap-southeast-2_synthetic'})
        for env in variants:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch('foodtech_business_api.withdrawal_identity_worker.boto3.Session') as session, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 2)
                session.assert_not_called()

    def test_main_reports_only_counts_and_fails_when_any_candidate_needs_attention(self):
        for state, expected in (('COGNITO_REMOVED', 0), ('BLOCKED', 1), ('RECONCILIATION_REQUIRED', 1)):
            worker, _ = self.worker(iter([{'results': [{'state': state, 'deletion_complete': False}], 'next_cursor': None}]))
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), \
                    patch('foodtech_business_api.withdrawal_identity_worker.configured_worker', return_value=worker), \
                    redirect_stdout(output):
                self.assertEqual(main(), expected)
            self.assertFalse(json.loads(output.getvalue())['deletion_complete'])

    def test_storage_failure_never_emits_sensitive_exception_or_success_summary(self):
        worker, _ = self.worker(iter([RuntimeError('SESSION#synthetic-secret')]))
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch('foodtech_business_api.withdrawal_identity_worker.configured_worker', return_value=worker), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('synthetic-secret', errors.getvalue())

    def test_missing_enrollment_gate_does_not_bootstrap_or_scan(self):
        env = {'FOODTECH_WITHDRAWAL_IDENTITY_WORKER_ENABLED': 'true',
               'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev',
               'FOODTECH_DELETION_REGISTRY_TABLE': 'foodtech-deletion-registry-dev',
               'FOODTECH_COGNITO_USER_POOL_ID': 'ap-southeast-2_synthetic'}
        with patch.dict(os.environ, env, clear=True), \
                patch('foodtech_business_api.withdrawal_identity_worker.boto3.Session') as session, \
                patch('foodtech_business_api.withdrawal_identity_worker.DynamoPrincipalEnrollment') as enrollment:
            enrollment.return_value.fence.side_effect = RuntimeError('gate unavailable')
            with self.assertRaises(RuntimeError):
                configured_worker()
            enrollment.return_value.bootstrap.assert_not_called()
            session.return_value.client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
