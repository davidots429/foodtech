import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, MagicMock, patch

import boto3
from botocore.stub import Stubber

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, 'path', [str(ROOT / 'infra/scripts'), *sys.path]):
    spec = importlib.util.spec_from_file_location('private_rest_initialize', ROOT / 'infra/scripts/private_rest_initialize.py')
    initialize = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = initialize
    spec.loader.exec_module(initialize)


class PrivateRestInitializationTests(unittest.TestCase):
    request = 'ca12998d-92bf-44be-8eb6-a6caa909b58a'

    @classmethod
    def setUpClass(cls):
        cls.value = initialize.generate()
        cls.stores = [initialize.Store(kind,
            f'arn:aws:secretsmanager:{initialize.REGION}:{initialize.ACCOUNT}:secret:foodtech/dev/private-rest-{kind}-{suffix}',
            f'arn:aws:kms:{initialize.REGION}:{initialize.ACCOUNT}:key/{key}')
            for kind, suffix, key in [('mock', 'aaaaaa', '11111111-1111-4111-8111-111111111111'),
                                      ('collector', 'bbbbbb', '22222222-2222-4222-8222-222222222222')]]
        cls.template = {'Description': 'SYNTHETIC reviewed template fixture', 'Resources': {
            store.kind.capitalize() + 'Key': {'Type': 'AWS::KMS::Key', 'Properties': {'KeyPolicy': initialize.expected_policy(store)}}
            for store in cls.stores}}
        cls.template['Resources'].update({store.kind.capitalize() + 'Secret': {
            'Type': 'AWS::SecretsManager::Secret', 'Properties': {'Name': store.name,
                'KmsKeyId': {'Fn::GetAtt': [store.kind.capitalize() + 'Key', 'Arn']}}} for store in cls.stores})
        cls.digest = initialize.canonical_hash(cls.template)

    def args(self, apply=False):
        result = ['--request-id', self.request, '--reviewed-template-sha256', self.digest]
        for store in self.stores:
            result += [f'--{store.kind}-secret-arn', store.arn, f'--{store.kind}-key-arn', store.key]
        return result + (['--apply'] if apply else [])

    def test_invalid_arguments_or_environment_never_load_credentials(self):
        for args, env in [(self.args() + ['--unknown', 'synthetic-credential-never-log'], {}),
                (self.args() + ['--max-seconds', '0'], {}),
                ([x.replace(self.request, 'not-a-uuid') for x in self.args()], {}),
                ([x.replace(initialize.ACCOUNT, '999999999999') for x in self.args()], {}),
                (self.args(), {'AWS_ACCESS_KEY_ID': 'synthetic-credential-never-log'}),
                (self.args(), {'AWS_PROFILE': 'other'})]:
            output = io.StringIO()
            with self.subTest(env=bool(env)), patch.dict(os.environ, env, clear=True), \
                    patch.object(initialize.boto3, 'Session') as session, contextlib.redirect_stderr(output):
                self.assertEqual(initialize.main(args), 2)
                session.assert_not_called()
            self.assertTrue('synthetic-credential-never-log' not in output.getvalue(), 'argument value leaked')

    def test_only_foodtech_login_owner_can_create_metadata_clients(self):
        for method in (None, 'env', 'shared-credentials-file', 'container-role'):
            session = MagicMock()
            session.get_credentials.return_value = None if method is None else SimpleNamespace(method=method)
            with self.subTest(method=method), patch.object(initialize.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
                initialize.VerifiedOperator()
            session.client.assert_not_called()
        session = MagicMock()
        session.get_credentials.return_value = SimpleNamespace(method='login')
        session.client.return_value.get_caller_identity.return_value = {'Account': initialize.ACCOUNT,
            'Arn': f'arn:aws:sts::{initialize.ACCOUNT}:assumed-role/foodtech-collection-task-dev/task'}
        with patch.object(initialize.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
            initialize.VerifiedOperator()
        self.assertEqual([c.args[0] for c in session.client.call_args_list], ['sts'])

    def flow(self, *, apply=False, existing=(False, False), failure=None):
        raw = boto3.Session(aws_access_key_id='test', aws_secret_access_key='test', region_name=initialize.REGION)
        clients = {name: raw.client(name, endpoint_url=f'https://{name}.{initialize.REGION}.amazonaws.com')
                   for name in ('sts', 'cloudformation', 'kms', 'secretsmanager')}
        stubs = {name: Stubber(client) for name, client in clients.items()}
        stubs['sts'].add_response('get_caller_identity', {'Account': initialize.ACCOUNT,
            'Arn': f'arn:aws:sts::{initialize.ACCOUNT}:assumed-role/AccountFullAccessRole/operator-session', 'UserId': 'SYNTHETIC'})
        cf, kms, sm = (stubs[name] for name in ('cloudformation', 'kms', 'secretsmanager'))
        outputs = [{'OutputKey': store.kind.capitalize() + suffix, 'OutputValue': value} for store in self.stores
                   for suffix, value in (('SecretArn', store.arn), ('SecretKeyArn', store.key))]
        cf.add_response('describe_stacks', {'Stacks': [{'StackName': initialize.STACK, 'StackStatus': 'CREATE_COMPLETE',
            'StackId': f'arn:aws:cloudformation:{initialize.REGION}:{initialize.ACCOUNT}:stack/{initialize.STACK}/test-id',
            'CreationTime': datetime.now(timezone.utc), 'Tags': [{'Key': 'Project', 'Value': 'foodtech'},
                {'Key': 'Environment', 'Value': 'dev'}], 'Outputs': outputs}]}, {'StackName': initialize.STACK})
        cf.add_response('get_template', {'TemplateBody': json.dumps(self.template if failure != 'template' else {})},
                        {'StackName': initialize.STACK, 'TemplateStage': 'Original'})
        stopped = failure == 'template'
        if not stopped:
            resources = [{'LogicalResourceId': store.kind.capitalize() + suffix, 'PhysicalResourceId': value,
                'ResourceType': resource_type, 'ResourceStatus': 'CREATE_COMPLETE', 'Timestamp': datetime.now(timezone.utc)}
                for store in self.stores for suffix, value, resource_type in (
                    ('Key', store.key.rsplit('/', 1)[1], 'AWS::KMS::Key'), ('Secret', store.arn, 'AWS::SecretsManager::Secret'))]
            cf.add_response('describe_stack_resources', {'StackResources': resources}, {'StackName': initialize.STACK})
            for store, exists in zip(self.stores, existing):
                sm.add_response('describe_secret', {'ARN': store.arn, 'Name': store.name, 'KmsKeyId': store.key,
                    'Tags': [{'Key': 'Project', 'Value': 'foodtech'}, {'Key': 'Environment', 'Value': 'dev'}]}, {'SecretId': store.arn})
                kms.add_response('describe_key', {'KeyMetadata': {'Arn': store.key, 'AWSAccountId': initialize.ACCOUNT,
                    'KeyId': store.key.rsplit('/', 1)[1], 'KeyManager': 'CUSTOMER', 'KeyState': 'Enabled', 'Enabled': True,
                    'KeySpec': 'SYMMETRIC_DEFAULT', 'KeyUsage': 'ENCRYPT_DECRYPT', 'Origin': 'AWS_KMS', 'MultiRegion': False}}, {'KeyId': store.key})
                kms.add_response('get_key_rotation_status', {'KeyRotationEnabled': True}, {'KeyId': store.key})
                policy = initialize.expected_policy(store)
                if failure == 'policy': policy['Statement'][1]['Condition']['StringLike']['kms:EncryptionContext:SecretARN'] = '*'
                kms.add_response('get_key_policy', {'Policy': json.dumps(policy)}, {'KeyId': store.key, 'PolicyName': 'default'})
                if failure == 'policy':
                    stopped = True
                    break
                params = {'SecretId': store.arn, 'IncludeDeprecated': True, 'MaxResults': 2}
                if failure == 'versions':
                    sm.add_response('list_secret_version_ids', {'Versions': [], 'NextToken': 'next-page'}, params)
                    sm.add_response('list_secret_version_ids', {'Versions': [{'VersionId': '12345678-1234-4234-8234-123456789abc',
                        'VersionStages': ['AWSCURRENT']}]}, {**params, 'NextToken': 'next-page'})
                    stopped = True
                    break
                sm.add_response('list_secret_version_ids', {'Versions': [{'VersionId': self.request,
                    'VersionStages': ['AWSCURRENT']}] if exists else []}, params)
        def read(store, value):
            sm.add_response('get_secret_value', {'ARN': store.arn, 'Name': store.name, 'VersionId': self.request,
                'VersionStages': ['AWSCURRENT'], 'SecretString': json.dumps(value)}, {'SecretId': store.arn, 'VersionId': self.request})
        if apply and not stopped and existing != (False, True):
            if existing[0]: read(self.stores[0], self.value.mock)
            for store, exists in zip(self.stores, existing):
                if exists: continue
                params = {'SecretId': store.arn, 'ClientRequestToken': self.request, 'SecretString': ANY}
                if failure == 'second-put' and store.kind == 'collector':
                    sm.add_client_error('put_secret_value', service_error_code='AccessDeniedException',
                        service_message='synthetic-credential-never-log', http_status_code=403, expected_params=params)
                    stopped = True
                    break
                sm.add_response('put_secret_value', {'ARN': store.arn, 'Name': store.name, 'VersionId': self.request,
                    'VersionStages': ['AWSCURRENT']}, params)
            if not stopped:
                read(self.stores[0], self.value.mock)
                read(self.stores[1], self.value.collector if failure != 'readback' else {})
        puts = []
        def record(params, **_):
            expected = self.value.mock if params['SecretId'] == self.stores[0].arn else self.value.collector
            puts.append(json.loads(params['SecretString']) == expected)
        clients['secretsmanager'].meta.events.register_first('before-parameter-build.secrets-manager.PutSecretValue', record)
        fake = MagicMock()
        fake.get_credentials.return_value = SimpleNamespace(method='login')
        fake.client.side_effect = lambda service, **kwargs: clients[service]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for stub in stubs.values(): stack.enter_context(stub)
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            factory = stack.enter_context(patch.object(initialize.boto3, 'Session', return_value=fake))
            generated = stack.enter_context(patch.object(initialize, 'generate', return_value=self.value))
            stack.enter_context(contextlib.redirect_stdout(out));stack.enter_context(contextlib.redirect_stderr(err))
            code = initialize.main(self.args(apply))
            for name, stub in stubs.items():
                try:
                    stub.assert_no_pending_responses()
                except AssertionError:
                    raise AssertionError(f'pending {name}; redacted result: {err.getvalue()}') from None
            factory.assert_called_once_with(profile_name='foodtech', region_name=initialize.REGION)
            if not apply or stopped and failure in ('policy', 'template', 'versions') or existing[0]:
                generated.assert_not_called()
        for call in fake.client.call_args_list:
            self.assertEqual(call.kwargs['endpoint_url'], f'https://{call.args[0]}.{initialize.REGION}.amazonaws.com')
            self.assertTrue(call.kwargs['verify'])
            self.assertEqual(call.kwargs['config'].proxies, {})
            self.assertTrue(call.kwargs['config'].ignore_configured_endpoint_urls)
        public = out.getvalue() + err.getvalue()
        for value in (self.value.mock['server_key_pem'], self.value.mock['client_secret'], 'synthetic-credential-never-log'):
            self.assertTrue(value not in public, 'sensitive value leaked')
        self.assertTrue(all(puts), 'uploaded pair differs')
        return code, json.loads(out.getvalue() or err.getvalue()), len(puts)

    def test_default_inspection_reads_only_metadata(self):
        code, result, count = self.flow()
        self.assertEqual((code, count), (0, 0))
        self.assertEqual(result['matching_version_counts'], [0, 0])
        self.assertFalse(result['values_read'])

    def test_explicit_initialization_writes_once_and_verifies_both_versions(self):
        code, result, count = self.flow(apply=True)
        self.assertEqual((code, count), (0, 2))
        self.assertEqual(result['initialized_secret_count'], 2)

    def test_partial_initialization_resumes_without_regenerating_or_rewriting_mock(self):
        code, result, count = self.flow(apply=True, existing=(True, False))
        self.assertEqual((code, count, result['resumed']), (0, 1, True))

    def test_complete_request_is_idempotent_without_any_writes(self):
        code, result, count = self.flow(apply=True, existing=(True, True))
        self.assertEqual((code, count, result['resumed']), (0, 0, True))

    def test_drift_and_foreign_paginated_history_never_generate_or_write(self):
        for failure in ('template', 'policy', 'versions'):
            with self.subTest(failure=failure):
                code, result, count = self.flow(apply=True, failure=failure)
                self.assertEqual((code, count), (1, 0))
                self.assertEqual(result['attempted_secret_writes'], 0)

    def test_inverted_partial_order_is_not_silently_repaired(self):
        code, result, count = self.flow(apply=True, existing=(False, True))
        self.assertEqual((code, count), (1, 0))
        self.assertTrue(result['reconciliation_required'])

    def test_failure_after_first_upload_reports_partial_progress_without_values(self):
        code, result, count = self.flow(apply=True, failure='second-put')
        self.assertEqual((code, count), (1, 2))
        self.assertEqual(result['acknowledged_secret_writes'], 1)
        self.assertTrue(result['reconciliation_required'])
        self.assertEqual(result['phase'], 'write_collector')

    def test_mismatched_readback_never_reports_completion(self):
        code, result, count = self.flow(apply=True, failure='readback')
        self.assertEqual((code, count), (1, 2))
        self.assertTrue(result['reconciliation_required'])

    def test_deadline_escapes_ordinary_retry_handlers(self):
        with self.assertRaises(initialize.InitializationDeadline):
            with initialize.deadline(1):
                while True:
                    try:
                        time.sleep(.02)
                    except Exception:
                        continue


if __name__ == '__main__':
    unittest.main()
