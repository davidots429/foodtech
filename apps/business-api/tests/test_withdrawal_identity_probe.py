import io
import json
import os
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from botocore.exceptions import ClientError
from foodtech_business_api.withdrawal_identity_probe import main


ACCOUNT = '123456789012'
PROBE = 'a55fe880-d10f-4f81-997f-d8a5c97ae147'
ENV = {'FOODTECH_IDENTITY_READ_PROBE_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
       'FOODTECH_PROBE_ACCOUNT': ACCOUNT, 'FOODTECH_PROBE_ID': PROBE}


class ReadOnlyResource:
    def __init__(self, *, accept_denied=False, denial_code='AccessDeniedException', fail_read=False):
        self.name = 'foodtech-auth-sessions-dev'
        self.meta = SimpleNamespace(client=self)
        self.calls = []
        self.accept_denied, self.denial_code, self.fail_read = accept_denied, denial_code, fail_read

    def Table(self, name):
        assert name == self.name
        return self

    def deny(self):
        if self.accept_denied:
            return {}
        raise ClientError({'Error': {'Code': self.denial_code, 'Message': 'synthetic-private-value'}}, 'GetItem')

    def get_item(self, **args):
        self.calls.append(('get_item', args))
        if args['Key']['pk'] == 'CONTROL#PRINCIPAL_ENROLLMENT':
            if self.fail_read:
                raise ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'synthetic-private-value'}}, 'GetItem')
            return {'Item': {'pk': 'CONTROL#PRINCIPAL_ENROLLMENT'}}
        return self.deny()

    def scan(self, **args):
        self.calls.append(('scan', args))
        return {'Items': [], 'LastEvaluatedKey': {'pk': 'synthetic-private-value'}}

    def transact_get_items(self, **args):
        self.calls.append(('transact_get_items', args))
        if args['TransactItems'][0]['Get']['TableName'] == 'foodtech-cooking-jobs-dev':
            return self.deny()
        return {'Responses': [{}, {}]}


class IdentityReadProbeTests(unittest.TestCase):
    def run_main(self, resource=None, *, env=ENV, caller=None, credential_method='container-role'):
        resource = resource or ReadOnlyResource()
        caller = caller or {'Account': ACCOUNT,
            'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/foodtech-withdrawal-identity-task-dev/probe-session'}
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch('foodtech_business_api.withdrawal_identity_probe.boto3.Session') as factory, \
                redirect_stdout(out), redirect_stderr(err):
            factory.return_value.client.return_value.get_caller_identity.return_value = caller
            factory.return_value.get_credentials.return_value = SimpleNamespace(method=credential_method)
            factory.return_value.resource.return_value = resource
            code = main()
            return code, out.getvalue(), err.getvalue(), factory

    def test_role_reads_and_exact_authorization_denials_without_record_values(self):
        resource = ReadOnlyResource()
        code, out, err, session = self.run_main(resource)
        self.assertEqual((code, err), (0, ''))
        result = json.loads(out)
        self.assertEqual(len(result['accepted_reads']), 3)
        self.assertEqual(len(result['expected_denials']), 3)
        self.assertFalse(result['identity_release_verified'])
        self.assertFalse(result['deletion_complete'])
        self.assertNotIn(PROBE, out)
        self.assertNotIn('synthetic-private-value', out)
        session.assert_called_once_with(region_name='ap-southeast-2')
        self.assertEqual(session.return_value.client.call_count, 1)
        self.assertEqual(session.return_value.client.call_args.args, ('sts',))
        self.assertTrue(session.return_value.client.call_args.kwargs['config'].ignore_configured_endpoint_urls)
        self.assertEqual(len(resource.calls), 6)
        self.assertEqual({name for name, _ in resource.calls}, {'get_item', 'transact_get_items', 'scan'})
        scan, = [args for name, args in resource.calls if name == 'scan']
        self.assertEqual((scan['Limit'], scan['Select']), (1, 'SPECIFIC_ATTRIBUTES'))
        self.assertEqual(set(scan['ExpressionAttributeNames'].values()), {'pk', 'epoch', 'withdrawal_request_id'})
        self.assertEqual(scan['ExpressionAttributeValues'], {':probe': 'USER#ROLEPROBE#' + PROBE})
        transaction, = [args for name, args in resource.calls if name == 'transact_get_items'
                        and len(args['TransactItems']) == 2]
        self.assertEqual(transaction['TransactItems'][1]['Get']['ProjectionExpression'], 'pk, sk')
        forbidden, = [args for name, args in resource.calls if name == 'transact_get_items'
                       and len(args['TransactItems']) == 1]
        self.assertEqual(forbidden['TransactItems'][0]['Get']['Key'],
                         {'pk': 'USER#ROLEPROBE#' + PROBE, 'sk': 'META'})

    def test_bad_config_is_rejected_before_sdk_or_credential_discovery(self):
        for env in ({}, {**ENV, 'AWS_EXECUTION_ENV': 'AWS_ECS_EC2'},
                    {**ENV, 'FOODTECH_PROBE_ACCOUNT': 'other'},
                    {**ENV, 'FOODTECH_PROBE_ID': 'not-a-uuid'},
                    {**ENV, 'FOODTECH_PROBE_ID': PROBE.upper()},
                    {**ENV, 'FOODTECH_IDENTITY_READ_PROBE_ENABLED': 'false'}):
            with self.subTest(env=env):
                code, out, _, factory = self.run_main(env=env)
                self.assertEqual((code, out), (2, ''))
                factory.assert_not_called()

    def test_developer_execution_other_account_and_execution_role_do_not_read(self):
        for caller in ({'Account': ACCOUNT, 'Arn': f'arn:aws:iam::{ACCOUNT}:user/developer'},
                       {'Account': '999999999999', 'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/foodtech-withdrawal-identity-task-dev/session'},
                       {'Account': ACCOUNT, 'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/foodtech-withdrawal-identity-execution-dev/session'}):
            with self.subTest(caller=caller):
                resource = ReadOnlyResource()
                code, out, err, factory = self.run_main(resource, caller=caller)
                self.assertEqual((code, out), (1, ''))
                self.assertNotIn(caller['Arn'], err)
                factory.return_value.resource.assert_not_called()
                self.assertEqual(resource.calls, [])

    def test_unexpected_allow_throttling_and_denied_positive_case_are_failures(self):
        for resource in (ReadOnlyResource(accept_denied=True),
                         ReadOnlyResource(denial_code='ProvisionedThroughputExceededException'),
                         ReadOnlyResource(denial_code='ResourceNotFoundException'),
                         ReadOnlyResource(fail_read=True)):
            code, out, err, _ = self.run_main(resource)
            self.assertEqual((code, out), (1, ''))
            self.assertNotIn('synthetic-private-value', err)

    def test_environment_profile_and_assumed_credentials_do_not_replace_ecs_identity(self):
        for method in ('env', 'shared-credentials-file', 'assume-role', 'login', None):
            with self.subTest(method=method):
                code, out, _, session = self.run_main(credential_method=method)
                self.assertEqual((code, out), (1, ''))
                session.return_value.client.assert_not_called()
                session.return_value.resource.assert_not_called()


if __name__ == '__main__':
    unittest.main()
