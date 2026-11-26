import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from foodtech_business_api.withdrawal_identity import IdentityReleaseCursor
from foodtech_business_api.withdrawal_identity_worker import sweep_identities, main, configured_worker, deadline


class IdentityWorkerTests(unittest.TestCase):
    def environment(self):
        return {'FOODTECH_WITHDRAWAL_IDENTITY_WORKER_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
            'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev',
            'FOODTECH_DELETION_REGISTRY_TABLE': 'foodtech-deletion-registry-dev',
            'FOODTECH_COGNITO_USER_POOL_ID': 'ap-southeast-2_synthetic',
            'FOODTECH_WITHDRAWAL_IDENTITY_TASK_ROLE_ARN': 'arn:aws:iam::123456789012:role/foodtech-withdrawal-identity-task-dev'}

    def session(self):
        session = MagicMock()
        session.get_credentials.return_value = SimpleNamespace(method='container-role')
        session.client.return_value.get_caller_identity.return_value = {
            'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/foodtech-withdrawal-identity-task-dev/synthetic-session'}
        return session

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
        session = self.session()
        with patch.dict(os.environ, self.environment(), clear=True), \
                patch('foodtech_business_api.withdrawal_identity_worker.boto3.Session', return_value=session), \
                patch('foodtech_business_api.withdrawal_identity_worker.DynamoPrincipalEnrollment') as enrollment:
            enrollment.return_value.fence.side_effect = RuntimeError('gate unavailable')
            with self.assertRaises(RuntimeError):
                configured_worker()
            enrollment.return_value.bootstrap.assert_not_called()
            self.assertEqual([x.args[0] for x in session.client.call_args_list], ['sts'])

    def test_missing_fargate_or_matching_role_fails_before_credential_discovery(self):
        variants = [('AWS_EXECUTION_ENV', ''), ('AWS_EXECUTION_ENV', 'AWS_Lambda_python3.12'),
            ('FOODTECH_WITHDRAWAL_IDENTITY_TASK_ROLE_ARN', ''),
            ('FOODTECH_WITHDRAWAL_IDENTITY_TASK_ROLE_ARN', 'arn:aws:iam::123456789012:role/foodtech-withdrawal-identity-task-demo'),
            ('FOODTECH_WITHDRAWAL_IDENTITY_TASK_ROLE_ARN', 'arn:aws:iam::123456789012:role/path/foodtech-withdrawal-identity-task-dev')]
        for name, value in variants:
            env = {**self.environment(), name: value}
            with self.subTest(name=name, value=value), patch.dict(os.environ, env, clear=True), \
                    patch('foodtech_business_api.withdrawal_identity_worker.boto3.Session') as session:
                with self.assertRaises(ValueError):
                    configured_worker()
                session.assert_not_called()

    def test_non_container_credentials_never_reach_sts_or_data_clients(self):
        for method in [None, 'env', 'login', 'assume-role', 'shared-credentials-file', 'iam-role']:
            session = self.session()
            session.get_credentials.return_value = None if method is None else SimpleNamespace(method=method)
            with self.subTest(method=method), patch.dict(os.environ, self.environment(), clear=True), \
                    patch('foodtech_business_api.withdrawal_identity_worker.boto3.Session', return_value=session):
                with self.assertRaises(ValueError):
                    configured_worker()
                session.client.assert_not_called()
                session.resource.assert_not_called()

    def test_container_provider_alone_does_not_prove_expected_role(self):
        identities = [
            {'Account': '999999999999', 'Arn': 'arn:aws:sts::999999999999:assumed-role/foodtech-withdrawal-identity-task-dev/session'},
            {'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/foodtech-withdrawal-identity-execution-dev/session'},
            {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/synthetic'},
            {'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/foodtech-withdrawal-identity-task-demo/session'}]
        for identity in identities:
            session = self.session()
            session.client.return_value.get_caller_identity.return_value = identity
            with self.subTest(identity=identity), patch.dict(os.environ, self.environment(), clear=True), \
                    patch('foodtech_business_api.withdrawal_identity_worker.boto3.Session', return_value=session):
                with self.assertRaises(ValueError):
                    configured_worker()
                self.assertEqual([x.args[0] for x in session.client.call_args_list], ['sts'])
                session.resource.assert_not_called()

    def test_verified_identity_shares_guarded_sdk_configuration_and_requires_enrollment(self):
        session = self.session()
        with patch.dict(os.environ, self.environment(), clear=True), \
                patch('foodtech_business_api.withdrawal_identity_worker.boto3.Session', return_value=session), \
                patch('foodtech_business_api.withdrawal_identity_worker.DynamoPrincipalEnrollment') as enrollment, \
                patch('foodtech_business_api.withdrawal_identity_worker.CognitoWithdrawalRelease') as worker:
            self.assertIs(configured_worker(), worker.return_value)
            enrollment.return_value.fence.assert_called_once_with()
            enrollment.return_value.bootstrap.assert_not_called()
            options = [x.kwargs['config'] for x in session.client.call_args_list + session.resource.call_args_list]
            self.assertTrue(all(x is options[0] for x in options))
            self.assertTrue(options[0].ignore_configured_endpoint_urls)
            self.assertEqual((options[0].connect_timeout, options[0].read_timeout), (5, 15))
            self.assertEqual([x.args[0] for x in session.client.call_args_list], ['sts', 'cognito-idp'])
            self.assertEqual(worker.call_args.kwargs['pool_id'], 'ap-southeast-2_synthetic')

    def test_invalid_wall_clock_bound_fails_before_clients(self):
        for seconds in ['0', '9', '601', 'not-an-integer']:
            with self.subTest(seconds=seconds), patch.dict(os.environ,
                    {**self.environment(), 'FOODTECH_WITHDRAWAL_IDENTITY_MAX_SECONDS': seconds}, clear=True), \
                    patch('foodtech_business_api.withdrawal_identity_worker.boto3.Session') as session, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 2)
                session.assert_not_called()

    def test_deadline_expires_without_waiting_and_restores_prior_handler(self):
        with patch('foodtech_business_api.withdrawal_identity_worker.signal.signal', return_value='prior') as handlers, \
                patch('foodtech_business_api.withdrawal_identity_worker.signal.alarm') as alarms:
            with self.assertRaises(TimeoutError):
                with deadline(10):
                    handler = handlers.call_args.args[1]
                    handler(None, None)
            self.assertEqual([x.args for x in alarms.call_args_list], [(10,), (0,)])
            self.assertEqual(handlers.call_args.args[1], 'prior')

    def test_initialization_timeout_fails_without_success_or_sensitive_output(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch('foodtech_business_api.withdrawal_identity_worker.configured_worker',
                    side_effect=TimeoutError('USER#synthetic-secret')), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('synthetic-secret', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
