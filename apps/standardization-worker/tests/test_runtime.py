import io
import json
import os
import signal
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from foodtech_standardization_worker import runtime


class RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(signal, 'SIGALRM'), 'Fargate Linux runtime deadline')
    def test_deadline_interrupts_blocked_processing_and_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(TimeoutError), runtime.deadline(1):
            time.sleep(3)
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)

    def env(self):
        account = '123456789012'
        return {'FOODTECH_STANDARDIZATION_RUNTIME_ENABLED': 'true',
            'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
            'FOODTECH_WORKER_ACCOUNT': account, 'FOODTECH_WORKER_STAGE': 'dev',
            'FOODTECH_WORKER_TASK_ROLE_ARN': f'arn:aws:iam::{account}:role/reviewed-worker',
            'FOODTECH_DATA_TABLE': 'foodtech-data-flow-dev', 'FOODTECH_COOKING_TABLE': 'foodtech-cooking-jobs-dev',
            'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev',
            'FOODTECH_ORDERED_QUEUE_URL': f'https://sqs.ap-southeast-2.amazonaws.com/{account}/foodtech-ordered-dev.fifo',
            'FOODTECH_STANDARDIZATION_DLQ_URL': f'https://sqs.ap-southeast-2.amazonaws.com/{account}/foodtech-standardizationdlq-dev.fifo'}

    def test_missing_opt_in_or_mismatched_project_never_creates_aws_session(self):
        env = self.env()
        for key in env:
            with self.subTest(key=key), patch.dict(os.environ, {**env, key: 'untrusted'}, clear=True), \
                    patch.object(runtime.boto3, 'Session') as session, redirect_stderr(io.StringIO()):
                self.assertEqual(runtime.main(), 2)
                session.assert_not_called()
        for value in ('0', '601', 'invalid'):
            with patch.dict(os.environ, {**env, 'FOODTECH_WORKER_MAX_SECONDS': value}, clear=True), \
                    patch.object(runtime.boto3, 'Session') as session, redirect_stderr(io.StringIO()):
                self.assertEqual(runtime.main(), 2)
                session.assert_not_called()

    def test_static_or_missing_credentials_are_rejected_before_any_aws_request(self):
        for credentials in (None, SimpleNamespace(method='env'), SimpleNamespace(method='login'),
                            SimpleNamespace(method='assume-role')):
            session = Mock()
            session.get_credentials.return_value = credentials
            with patch.object(runtime.boto3, 'Session', return_value=session), \
                    patch.object(runtime, 'DynamoStandardizationWorker') as worker:
                with self.assertRaises(ValueError):
                    runtime.configured_worker('123456789012', 'reviewed-worker')
                session.client.assert_not_called()
                worker.assert_not_called()

    def test_wrong_actual_role_or_project_is_rejected_before_data_clients(self):
        for identity in ({'Account': '999999999999', 'Arn': 'untrusted'},
                         {'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/admin/session'},
                         {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:role/reviewed-worker'}):
            session = Mock()
            session.get_credentials.return_value.method = 'container-role'
            session.client.return_value.get_caller_identity.return_value = identity
            with patch.object(runtime.boto3, 'Session', return_value=session), \
                    patch.object(runtime, 'DynamoStandardizationWorker') as worker:
                with self.assertRaises(ValueError):
                    runtime.configured_worker('123456789012', 'reviewed-worker')
                worker.assert_not_called()
                session.resource.assert_not_called()

    def test_verified_session_and_endpoint_protection_are_passed_to_all_worker_dependencies(self):
        session = Mock()
        session.get_credentials.return_value.method = 'container-role'
        session.client.return_value.get_caller_identity.return_value = {
            'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/reviewed-worker/ecs-session'}
        with patch.dict(os.environ, self.env(), clear=True), \
                patch.object(runtime.boto3, 'Session', return_value=session), \
                patch.object(runtime, 'DynamoStandardizationWorker') as worker:
            self.assertIs(runtime.configured_worker('123456789012', 'reviewed-worker'), worker.return_value)
            args = worker.call_args.kwargs
            self.assertIs(args['aws_session'], session)
            self.assertTrue(args['aws_config'].ignore_configured_endpoint_urls)
            self.assertEqual(args['aws_config'].read_timeout, 15)

    def test_output_contains_only_count_and_failures_do_not_expose_partial_results(self):
        for result in ([{'raw': 'must-stay-private'}], [], RuntimeError('must-stay-private'),
                       TimeoutError('must-stay-private'), [{}] * 11):
            worker = Mock()
            if isinstance(result, Exception):
                worker.run_once.side_effect = result
            else:
                worker.run_once.return_value = result
            output, errors = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, self.env(), clear=True), \
                    patch.object(runtime, 'configured_worker', return_value=worker), \
                    redirect_stdout(output), redirect_stderr(errors):
                status = runtime.main()
            self.assertNotIn('must-stay-private', output.getvalue() + errors.getvalue())
            if isinstance(result, list) and len(result) <= 10:
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(output.getvalue()), {'schema': 1, 'messages_processed': len(result), 'pass_finished': True})
            else:
                self.assertEqual(status, 1)
                self.assertEqual(output.getvalue(), '')

    def test_nested_bridges_reuse_the_verified_session_and_endpoint_configuration(self):
        session = Mock()
        session.get_credentials.return_value.method = 'container-role'
        session.client.return_value.get_caller_identity.return_value = {
            'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/reviewed-worker/ecs-session'}
        with patch.dict(os.environ, self.env(), clear=True), patch.object(runtime.boto3, 'Session', return_value=session) as factory:
            worker = runtime.configured_worker('123456789012', 'reviewed-worker')
            factory.assert_called_once_with(region_name='ap-southeast-2')
        self.assertIsNotNone(worker.process_bridge.restatement)
        self.assertIsNotNone(worker.jeyuk_job_bridge)
        configuration = session.client.call_args_list[0].kwargs['config']
        for call in session.client.call_args_list + session.resource.call_args_list:
            self.assertIs(call.kwargs['config'], configuration)


if __name__ == '__main__':
    unittest.main()
