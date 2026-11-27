import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from foodtech_business_api import meal_refresh_runtime as runtime
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.combi_refresh_worker import process_pending_page


class MealRefreshRuntimeTests(unittest.TestCase):
    def environment(self):
        return {'FOODTECH_MEAL_REFRESH_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
            'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev', 'FOODTECH_COOKING_TABLE': 'foodtech-cooking-jobs-dev',
            'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true',
            'FOODTECH_MEAL_REFRESH_TASK_ROLE_ARN': 'arn:aws:iam::123456789012:role/foodtech-meal-refresh-task-dev'}

    def session(self):
        session = MagicMock()
        session.get_credentials.return_value = SimpleNamespace(method='container-role')
        sts, client, resource = MagicMock(), MagicMock(), MagicMock()
        sts.get_caller_identity.return_value = {'Account': '123456789012',
            'Arn': 'arn:aws:sts::123456789012:assumed-role/foodtech-meal-refresh-task-dev/synthetic-session'}
        session.client.side_effect = lambda service, **_: sts if service == 'sts' else client
        session.resource.return_value = resource
        auth = {'TableStatus': 'ACTIVE', 'TableArn': 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-auth-sessions-dev',
            'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'}],
            'AttributeDefinitions': [{'AttributeName': 'pk', 'AttributeType': 'S'}]}
        cooking = {'TableStatus': 'ACTIVE', 'TableArn': 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-cooking-jobs-dev',
            'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
            'AttributeDefinitions': [{'AttributeName': n, 'AttributeType': 'S'} for n in ('pk', 'sk', 'aggregate_refresh_shard')],
            'GlobalSecondaryIndexes': [{'IndexName': 'MealAggregatePending', 'IndexStatus': 'ACTIVE',
                'KeySchema': [{'AttributeName': 'aggregate_refresh_shard', 'KeyType': 'HASH'}, {'AttributeName': 'pk', 'KeyType': 'RANGE'}],
                'Projection': {'ProjectionType': 'KEYS_ONLY'}}]}
        client.describe_table.side_effect = lambda TableName: {'Table': auth if TableName.endswith('sessions-dev') else cooking}
        return session, sts, client, resource, auth, cooking

    def test_invalid_opt_in_stage_role_mode_or_bounds_never_discovers_credentials(self):
        changes = [('FOODTECH_MEAL_REFRESH_ENABLED', ''), ('AWS_EXECUTION_ENV', ''),
            ('FOODTECH_AUTH_TABLE', 'foodtech-auth-sessions-demo'), ('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED', ''),
            ('FOODTECH_MEAL_REFRESH_TASK_ROLE_ARN', 'arn:aws:iam::123456789012:role/foodtech-meal-refresh-task-demo'),
            ('FOODTECH_MEAL_REFRESH_PAGE_SIZE', '0'), ('FOODTECH_MEAL_REFRESH_MAX_PAGES_PER_SHARD', '10001'),
            ('FOODTECH_MEAL_REFRESH_MAX_SECONDS', '601'), ('FOODTECH_MEAL_REFRESH_MAX_SECONDS', 'NaN')]
        for name, value in changes:
            with self.subTest(name=name), patch.dict(os.environ, {**self.environment(), name: value}, clear=True), \
                    patch.object(runtime.boto3, 'Session') as session, redirect_stderr(io.StringIO()):
                self.assertEqual(runtime.main(), 2)
                session.assert_not_called()

    def test_non_container_credentials_cannot_create_sts_or_data_clients(self):
        for method in (None, 'env', 'login', 'assume-role', 'shared-credentials-file', 'iam-role'):
            session, *_ = self.session()
            session.get_credentials.return_value = None if method is None else SimpleNamespace(method=method)
            with self.subTest(method=method), patch.dict(os.environ, self.environment(), clear=True), \
                    patch.object(runtime.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
                runtime.configured_worker()
            session.client.assert_not_called()
            session.resource.assert_not_called()

    def test_wrong_actual_account_role_or_stage_cannot_access_dynamodb(self):
        for account, arn in [('999999999999', 'arn:aws:sts::999999999999:assumed-role/foodtech-meal-refresh-task-dev/session'),
                ('123456789012', 'arn:aws:sts::123456789012:assumed-role/execution-dev/session'),
                ('123456789012', 'arn:aws:sts::123456789012:assumed-role/foodtech-meal-refresh-task-demo/session')]:
            session, sts, *_ = self.session()
            sts.get_caller_identity.return_value = {'Account': account, 'Arn': arn}
            with self.subTest(arn=arn), patch.dict(os.environ, self.environment(), clear=True), \
                    patch.object(runtime.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
                runtime.configured_worker()
            self.assertEqual([c.args[0] for c in session.client.call_args_list], ['sts'])
            session.resource.assert_not_called()

    def test_metadata_or_pending_index_mismatch_never_builds_store_or_queries(self):
        for kind in ('table', 'pk', 'index_missing', 'index_building', 'index_keys', 'index_projection'):
            session, _, client, resource, auth, cooking = self.session()
            if kind == 'table': auth['TableArn'] += '-other'
            if kind == 'pk': auth['AttributeDefinitions'][0]['AttributeType'] = 'N'
            if kind == 'index_missing': cooking['GlobalSecondaryIndexes'] = []
            if kind == 'index_building': cooking['GlobalSecondaryIndexes'][0]['Backfilling'] = True
            if kind == 'index_keys': cooking['GlobalSecondaryIndexes'][0]['KeySchema'][1]['AttributeName'] = 'sk'
            if kind == 'index_projection': cooking['GlobalSecondaryIndexes'][0]['Projection']['ProjectionType'] = 'ALL'
            with self.subTest(kind=kind), patch.dict(os.environ, self.environment(), clear=True), \
                    patch.object(runtime.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
                runtime.configured_worker()
            session.resource.assert_not_called()
            client.query.assert_not_called()

    def test_verified_dependencies_reach_real_store_without_new_default_session(self):
        session, _, client, resource, *_ = self.session()
        with patch.dict(os.environ, self.environment(), clear=True), patch.object(runtime.boto3, 'Session', return_value=session) as factory:
            store, auth = runtime.configured_worker()
        factory.assert_called_once_with(region_name='ap-southeast-2')
        self.assertIsInstance(store, DynamoCookingStore)
        self.assertIs(store.client, client)
        self.assertIs(auth, resource.Table.return_value)
        self.assertTrue(store.require_principal)
        self.assertEqual(store.auth_table_name, 'foodtech-auth-sessions-dev')
        configs = [c.kwargs['config'] for c in session.client.call_args_list] + [session.resource.call_args.kwargs['config']]
        self.assertTrue(all(c is configs[0] for c in configs))
        self.assertTrue(configs[0].ignore_configured_endpoint_urls)
        self.assertEqual(configs[0].proxies, {})
        self.assertEqual([c.kwargs['endpoint_url'] for c in session.client.call_args_list],
            ['https://sts.ap-southeast-2.amazonaws.com', 'https://dynamodb.ap-southeast-2.amazonaws.com'])
        for kwargs in ({'dynamodb_resource': resource}, {'dynamodb_client': client}):
            with self.assertRaises(ValueError):
                DynamoCookingStore('cooking', **kwargs)

    def test_counts_only_bounds_forwarded_and_failure_retains_nonzero_exit(self):
        result = {'processed': 1, 'partial': 0, 'failed': 1, 'pages': 16}
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {**self.environment(), 'FOODTECH_MEAL_REFRESH_PAGE_SIZE': '3',
                'FOODTECH_MEAL_REFRESH_MAX_PAGES_PER_SHARD': '2'}, clear=True), \
                patch.object(runtime, 'configured_worker', return_value=('store', 'auth')), \
                patch.object(runtime, 'sweep_pending', return_value=result) as sweep, redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(runtime.main(), 1)
        sweep.assert_called_once_with('store', 'auth', page_size=3, max_pages_per_shard=2)
        self.assertEqual(json.loads(output.getvalue()), result)
        self.assertEqual(errors.getvalue(), '')

    def test_deadline_during_a_marker_is_not_swallowed_or_reported_success(self):
        store = MagicMock()
        store.table.query.return_value = {'Items': [{}]}
        for failure in (TimeoutError('private-key'), runtime.MealRefreshDeadline('private-key')):
            with patch('foodtech_business_api.combi_refresh_worker.process_pending_marker', side_effect=failure):
                with self.assertRaises(type(failure)):
                    process_pending_page(store, object(), 0)
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), \
                patch.object(runtime, 'configured_worker', side_effect=runtime.MealRefreshDeadline('private-key')), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(runtime.main(), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('private-key', errors.getvalue())

    def test_deadline_covers_initialization_and_restores_signal(self):
        with patch.object(runtime.signal, 'signal', return_value='previous') as signal, patch.object(runtime.signal, 'alarm') as alarm:
            with runtime.deadline(60):
                handler = signal.call_args_list[0].args[1]
                with self.assertRaises(runtime.MealRefreshDeadline):
                    # Generic SDK retry handlers must not swallow the pass deadline.
                    try:
                        handler()
                    except Exception:
                        self.fail('deadline was caught as a retryable request error')
            self.assertEqual([c.args[0] for c in alarm.call_args_list], [60, 0])
            signal.assert_any_call(runtime.signal.SIGALRM, 'previous')

    def test_real_alarm_escapes_a_generic_sdk_retry_loop(self):
        script = '''
import time
from unittest.mock import patch
from foodtech_business_api import meal_refresh_runtime as runtime
real_deadline = runtime.deadline
def retrying_initialization():
    while True:
        try:
            time.sleep(60)
        except Exception:
            continue
with patch.object(runtime, 'configured_worker', side_effect=retrying_initialization), \\
     patch.object(runtime, 'deadline', side_effect=lambda _: real_deadline(1)):
    raise SystemExit(runtime.main())
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'Meal refresh pass failed; reconcile before retrying.\n')


if __name__ == '__main__':
    unittest.main()
