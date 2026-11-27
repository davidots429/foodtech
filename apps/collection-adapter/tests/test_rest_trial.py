import contextlib
from datetime import datetime, timezone
import io
import os
import signal
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch, ANY

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.stub import Stubber

from foodtech_collection_adapter import rest_trial as runtime
from foodtech_collection_adapter.rest import PollResult


class RestTrialTests(unittest.TestCase):
    def environment(self):
        return {'FOODTECH_REST_TRIAL_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
            'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI': '/v2/credentials/test', 'FOODTECH_STAGE': 'dev',
            'FOODTECH_COLLECTION_TASK_ROLE_ARN': 'arn:aws:iam::123456789012:role/foodtech-collection-task-dev',
            'FOODTECH_REST_TRIAL_ID': 'ca12998d-92bf-44be-8eb6-a6caa909b58a',
            'FOODTECH_REST_TRIAL_ATTEMPT': 'allowed', 'FOODTECH_REST_TRIAL_MAX_SECONDS': '60',
            'FOODTECH_DATA_TABLE': 'foodtech-data-flow-dev',
            'FOODTECH_INGESTION_DLQ_BUCKET': 'foodtech-ingestion-quarantine-123456789012-dev',
            'FOODTECH_ORDERED_QUEUE_URL': 'https://sqs.ap-southeast-2.amazonaws.com/123456789012/foodtech-ordered-dev.fifo',
            'FOODTECH_MOCK_BASE_URL': 'https://10.42.0.40:8443', 'FOODTECH_MOCK_CLIENT_ID': 'test-client',
            'FOODTECH_MOCK_CLIENT_SECRET': 'synthetic-secret-never-log', 'FOODTECH_MOCK_CA_FILE': '/test-ca.pem'}

    def trial(self):
        with patch.dict(os.environ, self.environment(), clear=True), patch.object(runtime.ssl, 'create_default_context'):
            return runtime.configuration()

    def test_invalid_configuration_never_discovers_credentials(self):
        for name, value in [('FOODTECH_REST_TRIAL_ENABLED', ''), ('FOODTECH_STAGE', 'demo'),
            ('AWS_EXECUTION_ENV', ''), ('AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', ''),
            ('AWS_PROFILE', 'foodtech'), ('AWS_ACCESS_KEY_ID', 'synthetic-never-log'),
            ('AWS_CONTAINER_CREDENTIALS_FULL_URI', 'http://localhost/credentials'),
            ('FOODTECH_COLLECTION_TASK_ROLE_ARN', 'arn:aws:iam::123456789012:role/foodtech-rest-mock-task-dev'),
            ('FOODTECH_REST_TRIAL_ID', 'not-a-uuid'), ('FOODTECH_REST_TRIAL_ID', 'ca12998d-92bf-34be-8eb6-a6caa909b58a'),
            ('FOODTECH_MOCK_BASE_URL', 'https://example.com:8443'),
            ('FOODTECH_MOCK_BASE_URL', 'https://10.42.0.2:8443'),
            ('FOODTECH_MOCK_BASE_URL', 'http://10.42.0.40:8443'),
            ('FOODTECH_MOCK_BASE_URL', 'https://user:password@10.42.0.40:8443'),
            ('FOODTECH_MOCK_BASE_URL', 'https://10.42.0.40:8443/other'),
            ('FOODTECH_DATA_TABLE', 'foodtech-data-flow-demo'),
            ('FOODTECH_INGESTION_DLQ_BUCKET', 'other'), ('FOODTECH_ORDERED_QUEUE_URL', 'other'),
            ('FOODTECH_MOCK_CA_FILE', ''), ('FOODTECH_REST_TRIAL_ATTEMPT', ''),
            ('FOODTECH_REST_TRIAL_MAX_SECONDS', '61')]:
            output = io.StringIO()
            with self.subTest(name=name, value=value), patch.dict(os.environ, {**self.environment(), name: value}, clear=True), \
                    patch.object(runtime.ssl, 'create_default_context'), patch.object(runtime.boto3, 'Session') as session, \
                    contextlib.redirect_stderr(output):
                self.assertEqual(runtime.main(['--trial']), 2)
                session.assert_not_called()
            self.assertNotIn('synthetic-never-log', output.getvalue())
            self.assertNotIn('password', output.getvalue())

    def test_only_actual_expected_container_identity_can_create_data_clients(self):
        trial = self.trial()
        for method in (None, 'env', 'login', 'assume-role', 'shared-credentials-file', 'iam-role'):
            session = MagicMock()
            session.get_credentials.return_value = None if method is None else SimpleNamespace(method=method)
            with self.subTest(method=method), patch.object(runtime.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
                runtime.verified_session(trial)
            session.client.assert_not_called()
        for account, arn in [('999999999999', 'arn:aws:sts::999999999999:assumed-role/foodtech-collection-task-dev/worker'),
                ('123456789012', 'arn:aws:sts::123456789012:assumed-role/foodtech-rest-mock-task-dev/worker')]:
            session = MagicMock()
            session.get_credentials.return_value = SimpleNamespace(method='container-role')
            session.client.return_value.get_caller_identity.return_value = {'Account': account, 'Arn': arn}
            with patch.object(runtime.boto3, 'Session', return_value=session), self.assertRaises(ValueError):
                runtime.verified_session(trial)
            self.assertEqual([call.args[0] for call in session.client.call_args_list], ['sts'])
            session.resource.assert_not_called()

    def test_verified_clients_share_fixed_endpoints_and_are_cached(self):
        session = MagicMock()
        session.get_credentials.return_value = SimpleNamespace(method='container-role')
        session.client.return_value.get_caller_identity.return_value = {'Account': '123456789012',
            'Arn': 'arn:aws:sts::123456789012:assumed-role/foodtech-collection-task-dev/worker'}
        with patch.object(runtime.boto3, 'Session', return_value=session):
            sdk = runtime.verified_session(self.trial())
        for service in ('s3', 'sqs', 'dynamodb'):
            self.assertIs(sdk.client(service), sdk.client(service))
        self.assertIs(sdk.resource('dynamodb'), sdk.resource('dynamodb'))
        for call in session.client.call_args_list + session.resource.call_args_list:
            config = call.kwargs['config']
            self.assertEqual(config.region_name, runtime.REGION)
            self.assertEqual(config.proxies, {})
            self.assertTrue(config.ignore_configured_endpoint_urls)
            self.assertEqual(config.account_id_endpoint_mode, 'disabled')
            self.assertEqual(call.kwargs['endpoint_url'], f'https://{call.args[0]}.{runtime.REGION}.amazonaws.com')
        with self.assertRaises(ValueError): sdk.client('secretsmanager')
        with self.assertRaises(ValueError): sdk.resource('s3')

    def flow(self, mode='allowed', *, denial=None, wrong_reply=False, existing=False):
        trial = self.trial()
        trial = runtime.Trial(**{**trial.__dict__, 'attempt': mode})
        with patch.dict(os.environ, {}, clear=True):
            raw = boto3.Session(aws_access_key_id='test', aws_secret_access_key='test', region_name=runtime.REGION)
            sdk = runtime.VerifiedSession(raw, Config(region_name=runtime.REGION))
            resource = sdk.resource('dynamodb')
            table = resource.Table(trial.table)
            ddb = Stubber(resource.meta.client)
            low = Stubber(sdk.client('dynamodb'))
            sqs = Stubber(sdk.client('sqs'))
            s3 = Stubber(sdk.client('s3'))
        def get(key, item=None):
            marshaled = {k: TypeSerializer().serialize(v) for k, v in item.items()} if item else None
            ddb.add_response('get_item', {} if marshaled is None else {'Item': marshaled},
                {'TableName': trial.table, 'Key': key, 'ConsistentRead': True})
        get(trial.cursor, {**trial.cursor, 'cursor': 1} if existing else None)
        get(trial.manifest)
        if not existing:
            get(trial.cursor)
            if not wrong_reply:
                digest = runtime.hashlib.sha256(runtime.PAYLOAD).hexdigest()
                key = f'ingestion/REST/{runtime.PROFILE}/{trial.event_id}/{digest}.bin'
                params = {'Bucket': trial.bucket, 'Key': key, 'Body': runtime.PAYLOAD,
                    'ContentType': 'application/octet-stream', 'ServerSideEncryption': 'AES256'}
                if denial:
                    s3.add_client_error('put_object', service_error_code=denial, http_status_code=403, expected_params=params)
                    if mode == 'blocked' and denial == 'AccessDenied':
                        get(trial.cursor); get(trial.manifest)
                else:
                    s3.add_response('put_object', {}, params)
                    ddb.add_response('put_item', {}, {'TableName': trial.table, 'Item': ANY,
                        'ConditionExpression': 'attribute_not_exists(pk)'})
                    ddb.add_response('update_item', {}, {'TableName': trial.table, 'Key': trial.cursor,
                        'UpdateExpression': 'SET #cursor = :cursor, #streams = :streams',
                        'ExpressionAttributeNames': {'#cursor': 'cursor', '#streams': 'streams'},
                        'ExpressionAttributeValues': {':cursor': 1, ':streams': []}})
                    if mode == 'allowed':
                        manifest = {**trial.manifest, 'ingestion_event_id': trial.event_id, 'bucket': trial.bucket,
                            'sha256': digest, 'object_key': key, 'ingestion_failure_reason_code': 'UNSUPPORTED_ENCODING',
                            'missing_components': [], 'protocol': 'REST', 'profile_id': runtime.PROFILE,
                            'instance_id': runtime.INSTANCE, 'cursor_key': trial.cursor_key, 'source_position': 0,
                            'provenance': 'SYNTHETIC'}
                        get(trial.cursor, {**trial.cursor, 'cursor': 1, 'streams': []}); get(trial.manifest, manifest)
        client = MagicMock()
        client.poll.return_value = PollResult(('unexpected-valid-source',) if wrong_reply else (runtime.PAYLOAD,),
                                               1, True, True)
        stdout, stderr = io.StringIO(), io.StringIO()
        with ddb, low, s3, sqs, patch.dict(os.environ, self.environment(), clear=True), \
                patch.object(runtime, 'configuration', return_value=trial), \
                patch.object(runtime, 'verified_session', return_value=sdk), \
                patch.object(runtime.boto3, 'Session', side_effect=AssertionError('unverified SDK factory used')), \
                patch.object(runtime.collector, 'RestMockClient', return_value=client), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = runtime.main(['--trial'])
            for stub in (ddb, low, s3, sqs): stub.assert_no_pending_responses()
        self.assertNotIn('synthetic-secret-never-log', stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_real_collector_code_keeps_cursor_absent_after_sdk_denial(self):
        code, out, _ = self.flow('blocked', denial='AccessDenied')
        self.assertEqual(code, 0)
        self.assertIn('"storage_denial_verified": true', out)

    def test_real_collector_code_writes_manifest_before_cursor_on_success(self):
        code, out, _ = self.flow()
        self.assertEqual(code, 0)
        self.assertIn('"quarantine_manifest_verified": true', out)

    def test_wrong_payload_existing_rows_wrong_error_or_unexpected_acceptance_fail(self):
        for kwargs in ({'wrong_reply': True}, {'existing': True},
                {'mode': 'blocked', 'denial': 'NoSuchBucket'}, {'mode': 'allowed', 'denial': 'AccessDenied'}, {'mode': 'blocked'}):
            with self.subTest(kwargs=kwargs):
                code, out, _ = self.flow(**kwargs)
                self.assertEqual(code, 1)
                self.assertNotIn('"storage_denial_verified": true', out)

    def test_alarm_escapes_an_ordinary_exception_retry_loop_during_identity_discovery(self):
        def retry_forever(*_):
            while True:
                try: time.sleep(.01)
                except Exception: pass
        original = signal.getsignal(signal.SIGALRM)
        actual_deadline = runtime.deadline
        start = time.monotonic()
        with patch.object(runtime, 'configuration', return_value=self.trial()), \
                patch.object(runtime, 'verified_session', side_effect=retry_forever), \
                patch.object(runtime, 'deadline', side_effect=lambda _: actual_deadline(1)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runtime.main(['--trial']), 1)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(signal.getsignal(signal.SIGALRM), original)


if __name__ == '__main__':
    unittest.main()
