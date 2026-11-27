import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import pickle
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_business_api.combi_read_broker import CombiReadBroker, issue_authenticated_combi_reads
from foodtech_business_api.recipe_scope import RecipeScopeDenied
import test_combi_read_scope as fixtures


class CombiReadBrokerTests(unittest.TestCase):
    def setUp(self):
        self.data = fixtures.CombiReadScopeTests(); self.data.setUp()
        self.scope = self.data.resolve()
        self.now = datetime.fromtimestamp(fixtures.fixtures.NOW, timezone.utc)
        self.sts, self.client = Mock(), Mock()
        self.sdk_session = Mock()
        self.sdk_session.get_credentials.return_value = SimpleNamespace(method='explicit')
        self.sdk_session.client.return_value = self.client
        self.factory = Mock(return_value=self.sdk_session)
        self.broker = CombiReadBroker(account='123456789012', stage='dev',
            read_role_arn='arn:aws:iam::123456789012:role/foodtech-combi-source-read-dev',
            sts=self.sts, session_factory=self.factory, clock=lambda: self.now)
        self.sts.assume_role.side_effect = self.response

    def response(self, **args):
        shape = get_session().get_service_model('sts').operation_model('AssumeRole').input_shape
        validate_parameters(args, shape)
        return {'AssumedRoleUser': {'Arn': 'arn:aws:sts::123456789012:assumed-role/foodtech-combi-source-read-dev/'+args['RoleSessionName']},
            'Credentials': {'AccessKeyId': 'synthetic-access-key', 'SecretAccessKey': 'synthetic-not-a-real-secret',
                'SessionToken': 'synthetic-not-a-real-token', 'Expiration': self.now+timedelta(seconds=900)}}

    def issue(self):
        return self.broker.issue(self.scope, execution_reader=self.data.reader, history_authority=self.data.data.authority)

    def test_lease_issues_only_exact_four_key_policy_and_fixed_sts_parameters(self):
        lease = self.issue()
        args = self.sts.assume_role.call_args.kwargs
        self.assertEqual(set(args), {'RoleArn', 'RoleSessionName', 'DurationSeconds', 'Policy'})
        self.assertEqual(args['DurationSeconds'], 900)
        self.assertTrue(args['RoleSessionName'].startswith('combi-'))
        policy = json.loads(args['Policy'])
        self.assertLessEqual(len(args['Policy']), 2048)
        self.assertEqual(len(policy['Statement']), 1)
        statement = policy['Statement'][0]
        self.assertEqual(statement['Action'], ['dynamodb:GetItem', 'dynamodb:Query'])
        self.assertEqual(statement['Resource'], 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-cooking-jobs-dev')
        self.assertEqual(statement['Condition']['ForAllValues:StringEquals']['dynamodb:LeadingKeys'], list(self.scope.read_partitions))
        self.assertEqual(statement['Condition']['Null'], {'dynamodb:LeadingKeys': 'false'})
        self.assertEqual(statement['Condition']['StringEquals'], {'aws:RequestedRegion': 'ap-southeast-2'})
        self.assertIs(lease.client(self.scope), self.client)
        args = self.sdk_session.client.call_args
        self.assertEqual(args.args, ('dynamodb',))
        self.assertEqual(args.kwargs['endpoint_url'], 'https://dynamodb.ap-southeast-2.amazonaws.com')
        self.assertTrue(args.kwargs['config'].ignore_configured_endpoint_urls)
        self.assertEqual(args.kwargs['config'].proxies, {})
        self.assertEqual(self.factory.call_args.kwargs['region_name'], 'ap-southeast-2')

    def test_lease_is_opaque_non_serializable_and_closed_clients_cannot_be_reissued(self):
        lease = self.issue()
        self.assertEqual(repr(lease), 'CombiReadLease()')
        self.assertNotIn(self.scope.owner, repr(lease))
        with self.assertRaises(TypeError): pickle.dumps(lease)
        with self.assertRaises(TypeError): deepcopy(lease)
        lease.client(self.scope)
        lease.close(); lease.close()
        self.client.close.assert_called_once()
        with self.assertRaises(RecipeScopeDenied): lease.client(self.scope)

    def test_changed_execution_or_whole_history_before_issue_never_assumes_role(self):
        self.data.snapshot = replace(self.data.snapshot, version=2)
        with self.assertRaises(RecipeScopeDenied): self.issue()
        self.sts.assume_role.assert_not_called()
        self.data.snapshot = replace(self.data.snapshot, version=1)
        self.data.data.history['revision'] = 13
        with self.assertRaises(RecipeScopeDenied): self.issue()
        self.sts.assume_role.assert_not_called()

    def test_changed_history_during_sts_does_not_release_credentials(self):
        def changed(**args):
            response = self.response(**args)
            self.data.data.history['revision'] = 13
            return response
        self.sts.assume_role.side_effect = changed
        with self.assertRaises(RecipeScopeDenied): self.issue()
        self.factory.assert_not_called()

    def test_wrong_role_expiry_or_credential_response_is_denied(self):
        for case in ('role', 'expired', 'long', 'naive', 'empty', 'non_explicit'):
            with self.subTest(case=case):
                def invalid(**args):
                    response = self.response(**args)
                    if case == 'role': response['AssumedRoleUser']['Arn'] += '-other'
                    elif case == 'expired': response['Credentials']['Expiration'] = self.now+timedelta(seconds=29)
                    elif case == 'long': response['Credentials']['Expiration'] = self.now+timedelta(hours=1)
                    elif case == 'naive': response['Credentials']['Expiration'] = self.now.replace(tzinfo=None)
                    elif case == 'empty': response['Credentials']['SessionToken'] = ''
                    return response
                self.sts.assume_role.side_effect = invalid
                self.sdk_session.get_credentials.return_value.method = 'env' if case == 'non_explicit' else 'explicit'
                with self.assertRaises(RecipeScopeDenied): self.issue()

    def test_local_expiry_history_deadline_clock_rollback_and_changed_scope_block_client(self):
        lease = self.issue()
        with self.assertRaises(RecipeScopeDenied): lease.client(replace(self.scope, execution_version=2))
        self.now += timedelta(seconds=1)
        lease.client(self.scope)
        self.now -= timedelta(seconds=1)
        with self.assertRaises(RecipeScopeDenied): lease.client(self.scope)
        self.now += timedelta(seconds=901)
        with self.assertRaises(RecipeScopeDenied): lease.client(self.scope)

    def test_scope_policy_stays_below_limit_for_maximum_length_owner(self):
        owner = 'a'*128
        witness = replace(self.scope.history, owner=owner)
        scope = replace(self.scope, owner=owner, history=witness)
        self.assertLessEqual(len(self.broker.session_policy(scope)), 2048)
        with self.assertRaises(RecipeScopeDenied): self.broker.session_policy(SimpleNamespace(**vars(scope)))

    def test_local_deadline_never_outlives_protected_writer_observation(self):
        until = int(self.now.timestamp())+100
        self.data.data.writer['valid_until'] = until
        self.scope = replace(self.scope, history=replace(self.scope.history, writer_valid_until=until))
        lease = self.issue()
        self.assertEqual(lease.expires_at, self.now+timedelta(seconds=100))
        self.now += timedelta(seconds=100)
        with self.assertRaises(RecipeScopeDenied): lease.client(self.scope)


class AuthenticatedCombiLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_sdk_await_disposes_its_eventual_unpublished_result(self):
        data = CombiReadBrokerTests(); data.setUp()
        started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        lease = Mock(); lease.close.side_effect = closed.set
        async def issue(*args, **kwargs):
            started.set()
            await release.wait()
            return lease
        with patch('foodtech_business_api.combi_read_broker.authenticated_combi_read_scope', AsyncMock(return_value=data.scope)), \
             patch('foodtech_business_api.combi_read_broker.asyncio.to_thread', side_effect=issue):
            task = asyncio.create_task(issue_authenticated_combi_reads(object(), data.scope.execution_id,
                broker=data.broker, execution_reader=data.data.reader, history_authority=data.data.data.authority))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            release.set()
            await asyncio.wait_for(closed.wait(), 1)
            lease.close.assert_called_once()

    async def test_second_auth_failure_or_cancellation_disposes_unreleased_lease(self):
        data = CombiReadBrokerTests(); data.setUp()
        for error in (RecipeScopeDenied('changed'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                lease = Mock()
                data.broker.issue = Mock(return_value=lease)
                with patch('foodtech_business_api.combi_read_broker.authenticated_combi_read_scope', AsyncMock(side_effect=[data.scope, error])), \
                     patch('foodtech_business_api.combi_read_broker.asyncio.to_thread', AsyncMock(return_value=lease)):
                    with self.assertRaises(type(error)):
                        await issue_authenticated_combi_reads(object(), data.scope.execution_id, broker=data.broker,
                            execution_reader=data.data.reader, history_authority=data.data.data.authority)
                    lease.close.assert_called_once()
