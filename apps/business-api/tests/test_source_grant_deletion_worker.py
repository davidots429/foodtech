import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from foodtech_business_api import source_grant_deletion_worker as runtime
from foodtech_business_api.source_read_registrar import SourceGrantConflict


class SourceGrantWorkerTests(unittest.TestCase):
    def candidate(self):
        return {'pk': 'USER#opaque_subject', 'epoch': Decimal(3), 'withdrawal_request_id': str(uuid4())}

    def result(self, remaining, state='GRANT_REMOVED'):
        return {'state': state, 'remaining_upper_bound': remaining, 'deletion_complete': False}

    def test_empty_intermediate_page_keeps_sensitive_cursor_only_in_memory(self):
        table, deletion = MagicMock(), MagicMock()
        key = {'pk': 'SESSION#synthetic-secret'}
        row = self.candidate()
        table.scan.side_effect = [{'Items': [], 'LastEvaluatedKey': key}, {'Items': [row]}]
        deletion.step.side_effect = [self.result(1), self.result(0), self.result(0, 'CATALOG_EMPTY_OBSERVED')]
        result = runtime.sweep_grants(table, deletion, clock=lambda: 1000)
        self.assertEqual(result['delete_acknowledged'], 2)
        self.assertTrue(result['scan_finished'])
        self.assertFalse(result['deletion_complete'])
        self.assertNotIn('synthetic-secret', repr(result))
        self.assertEqual(table.scan.call_args_list[1].kwargs['ExclusiveStartKey'], key)
        self.assertEqual(deletion.step.call_args.kwargs, {
            'subject': 'opaque_subject', 'request_id': row['withdrawal_request_id'], 'expected_epoch': 3})
        self.assertEqual(table.scan.call_args.kwargs['Select'], 'SPECIFIC_ATTRIBUTES')

    def test_mutation_budget_and_conflict_never_claim_whole_erasure(self):
        table, deletion = MagicMock(), MagicMock()
        table.scan.return_value = {'Items': [self.candidate()]}
        deletion.step.return_value = self.result(3)
        result = runtime.sweep_grants(table, deletion, max_changes=2, clock=lambda: 1000)
        self.assertEqual(deletion.step.call_count, 2)
        self.assertTrue(result['budget_exhausted'])
        self.assertFalse(result['scan_finished'])
        deletion.step.side_effect = SourceGrantConflict()
        result = runtime.sweep_grants(table, deletion, clock=lambda: 1000)
        self.assertEqual(result['blocked'], 1)
        self.assertFalse(result['deletion_complete'])

    def test_invalid_candidate_cycle_or_completion_claim_is_rejected(self):
        variants = [({'Items': [{**self.candidate(), 'epoch': value}]}, self.result(0))
                    for value in (True, Decimal('1.5'), 0)]
        variants += [({'Items': [self.candidate()]}, {**self.result(0), 'deletion_complete': True}),
                     ({'Items': [], 'LastEvaluatedKey': 'invalid'}, self.result(0))]
        for page, answer in variants:
            table, deletion = MagicMock(), MagicMock()
            table.scan.return_value, deletion.step.return_value = page, answer
            with self.subTest(page=page), self.assertRaises(ValueError):
                runtime.sweep_grants(table, deletion, clock=lambda: 1000)
        table = MagicMock()
        table.scan.return_value = {'Items': [], 'LastEvaluatedKey': {'pk': 'SESSION#synthetic-secret'}}
        with self.assertRaises(ValueError):
            runtime.sweep_grants(table, MagicMock(), clock=lambda: 1000)

    def environment(self):
        return {'FOODTECH_SOURCE_GRANT_DELETION_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
            'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev',
            'FOODTECH_SOURCE_ACCESS_TABLE': 'foodtech-source-access-dev',
            'FOODTECH_DELETION_REGISTRY_TABLE': 'foodtech-deletion-registry-dev',
            'FOODTECH_SOURCE_GRANT_DELETION_ROLE_ARN':
                'arn:aws:iam::123456789012:role/foodtech-source-grant-deletion-task-dev'}

    def session(self):
        session = MagicMock()
        session.get_credentials.return_value = SimpleNamespace(method='container-role')
        sts, ddb = MagicMock(), MagicMock()
        sts.get_caller_identity.return_value = {'Account': '123456789012',
            'Arn': 'arn:aws:sts::123456789012:assumed-role/foodtech-source-grant-deletion-task-dev/session'}
        session.client.side_effect = lambda service, **_: {'sts': sts, 'dynamodb': ddb}[service]
        def describe(*, TableName):
            keys = [('pk', 'HASH')] + ([] if 'auth-sessions' in TableName else [('sk', 'RANGE')])
            return {'Table': {'TableStatus': 'ACTIVE',
                'TableArn': 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/' + TableName,
                'KeySchema': [{'AttributeName': k, 'KeyType': t} for k, t in keys],
                'AttributeDefinitions': [{'AttributeName': k, 'AttributeType': 'S'} for k, _ in keys]}}
        ddb.describe_table.side_effect = describe
        return session, sts, ddb

    def test_configuration_is_closed_before_credential_discovery(self):
        for change in ({'FOODTECH_SOURCE_GRANT_DELETION_ENABLED': ''}, {'AWS_EXECUTION_ENV': ''},
                       {'FOODTECH_SOURCE_ACCESS_TABLE': 'foodtech-source-access-demo'},
                       {'FOODTECH_SOURCE_GRANT_DELETION_ROLE_ARN': 'arn:aws:iam::123456789012:role/other'}):
            with self.subTest(change=change), patch.dict(os.environ, {**self.environment(), **change}, clear=True), \
                    patch.object(runtime.boto3, 'Session') as session, self.assertRaises(ValueError):
                runtime.configured_worker()
            session.assert_not_called()

    def test_profile_credentials_or_wrong_role_never_reach_data_clients(self):
        for provider in ('env', 'login', 'assume-role'):
            session, _, _ = self.session()
            session.get_credentials.return_value.method = provider
            with patch.dict(os.environ, self.environment(), clear=True), \
                    patch.object(runtime.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
                runtime.configured_worker()
            session.client.assert_not_called()
        session, sts, _ = self.session()
        sts.get_caller_identity.return_value['Arn'] = 'arn:aws:sts::123456789012:assumed-role/other/session'
        with patch.dict(os.environ, self.environment(), clear=True), \
                patch.object(runtime.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
            runtime.configured_worker()
        self.assertEqual([call.args[0] for call in session.client.call_args_list], ['sts'])

    def test_verified_role_checks_all_tables_and_enrollment_with_shared_guarded_config(self):
        session, _, ddb = self.session()
        with patch.dict(os.environ, self.environment(), clear=True), \
                patch.object(runtime.boto3, 'Session', return_value=session), \
                patch.object(runtime, 'DynamoPrincipalEnrollment') as enrollment:
            table, worker = runtime.configured_worker()
        self.assertIs(worker.client, ddb)
        self.assertEqual(ddb.describe_table.call_count, 3)
        enrollment.return_value.fence.assert_called_once_with()
        self.assertEqual([call.args[0] for call in session.client.call_args_list], ['sts', 'dynamodb'])
        config = session.client.call_args_list[0].kwargs['config']
        self.assertEqual(config.proxies, {})
        self.assertTrue(config.ignore_configured_endpoint_urls)
        self.assertEqual(config.account_id_endpoint_mode, 'disabled')
        self.assertIs(session.resource.call_args.kwargs['config'], config)
        self.assertEqual(session.resource.call_args.kwargs['endpoint_url'], 'https://dynamodb.ap-southeast-2.amazonaws.com')

    def test_wrong_table_or_missing_enrollment_never_constructs_mutator(self):
        session, _, ddb = self.session()
        ddb.describe_table.side_effect = None
        ddb.describe_table.return_value = {'Table': {'TableStatus': 'ACTIVE', 'TableArn': 'foreign'}}
        with patch.dict(os.environ, self.environment(), clear=True), \
                patch.object(runtime.boto3, 'Session', return_value=session), \
                patch.object(runtime, 'DynamoSourceGrantDeletion') as mutation, self.assertRaises(ValueError):
            runtime.configured_worker()
        mutation.assert_not_called()
        session.resource.assert_not_called()
        session, _, _ = self.session()
        with patch.dict(os.environ, self.environment(), clear=True), \
                patch.object(runtime.boto3, 'Session', return_value=session), \
                patch.object(runtime, 'DynamoPrincipalEnrollment') as enrollment, \
                patch.object(runtime, 'DynamoSourceGrantDeletion') as mutation:
            enrollment.return_value.fence.side_effect = RuntimeError('missing gate')
            with self.assertRaises(RuntimeError):
                runtime.configured_worker()
        mutation.assert_not_called()

    def test_main_never_prints_discovery_keys_or_sdk_errors_and_defaults_disabled(self):
        errors, output = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime.boto3, 'Session') as session, \
                redirect_stderr(errors), redirect_stdout(output):
            self.assertEqual(runtime.main(), 2)
        session.assert_not_called()
        self.assertEqual(output.getvalue(), '')
        table, deletion = MagicMock(), MagicMock()
        table.scan.side_effect = RuntimeError('SESSION#synthetic-secret')
        with patch.object(runtime, 'configured_worker', return_value=(table, deletion)), \
                redirect_stderr(errors), redirect_stdout(output):
            self.assertEqual(runtime.main(), 1)
        self.assertNotIn('synthetic-secret', errors.getvalue())
        self.assertEqual(output.getvalue(), '')
        table.scan.side_effect = None
        table.scan.return_value = {'Items': []}
        with patch.object(runtime, 'configured_worker', return_value=(table, deletion)), redirect_stdout(output):
            self.assertEqual(runtime.main(), 0)
        self.assertFalse(json.loads(output.getvalue())['deletion_complete'])

    def test_invalid_bounds_fail_before_credentials_and_partial_budget_is_nonzero(self):
        for field in ('FOODTECH_SOURCE_GRANT_PAGE_SIZE', 'FOODTECH_SOURCE_GRANT_MAX_PAGES',
                      'FOODTECH_SOURCE_GRANT_MAX_CHANGES', 'FOODTECH_SOURCE_GRANT_MAX_SECONDS'):
            with patch.dict(os.environ, {field: '0'}, clear=True), \
                    patch.object(runtime.boto3, 'Session') as session, redirect_stderr(io.StringIO()):
                self.assertEqual(runtime.main(), 2)
                session.assert_not_called()
        table, deletion, output = MagicMock(), MagicMock(), io.StringIO()
        table.scan.return_value = {'Items': [self.candidate()]}
        deletion.step.return_value = self.result(1)
        with patch.dict(os.environ, {'FOODTECH_SOURCE_GRANT_MAX_CHANGES': '1'}, clear=True), \
                patch.object(runtime, 'configured_worker', return_value=(table, deletion)), redirect_stdout(output):
            self.assertEqual(runtime.main(), 1)
        self.assertTrue(json.loads(output.getvalue())['budget_exhausted'])
        self.assertEqual(deletion.step.call_count, 1)
