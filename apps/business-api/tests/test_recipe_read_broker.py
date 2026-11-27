import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
import pickle
import time
from boto3.dynamodb.types import TypeSerializer
from fastapi.encoders import jsonable_encoder

from foodtech_business_api.recipe_scope import RecipeReadScope, RecipeScopeDenied
from foodtech_business_api.recipe_read_broker import RecipeReadBroker, RecipeReadLease, configured_broker, issue_authenticated_reads
from foodtech_business_api.source_read_authority import DynamoSourceReadAuthority


class RecipeReadBrokerTests(unittest.TestCase):
    def setUp(self):
        self.scope = RecipeReadScope('opaque_subject', str(uuid4()),
            tuple(f'SCOPE#USER#opaque_subject#JOB#{uuid4()}' for _ in range(2)),
            ('JEYUK#jeyuk-current', 'JEYUK#jeyuk-prior'), (2, 1), (12, 11), 'PENDING', 1, 0)
        self.sts, self.sessions = MagicMock(), MagicMock()
        self.role = 'arn:aws:iam::123456789012:role/foodtech-recipe-source-read-dev'
        self.broker = RecipeReadBroker(account='123456789012', stage='dev',
            read_role_arn=self.role, sts=self.sts, session_factory=self.sessions)

        def assumed(**kwargs):
            return {'AssumedRoleUser': {'Arn': 'arn:aws:sts::123456789012:assumed-role/' +
                'foodtech-recipe-source-read-dev/' + kwargs['RoleSessionName']},
                'Credentials': {'AccessKeyId': 'synthetic-access', 'SecretAccessKey': 'synthetic-secret',
                    'SessionToken': 'synthetic-session', 'Expiration': datetime.now(timezone.utc) + timedelta(seconds=899)}}

        self.sts.assume_role.side_effect = assumed

    def test_policy_names_only_exact_job_and_source_keys_and_regional_tables(self):
        policy = json.loads(self.broker.session_policy(self.scope))
        self.assertEqual(policy['Version'], '2012-10-17')
        cooking, data = policy['Statement']
        for statement in policy['Statement']:
            self.assertEqual(statement['Action'], ['dynamodb:GetItem', 'dynamodb:Query'])
            self.assertNotIn('*', statement['Resource'])
            self.assertEqual(statement['Condition']['Null'], {'dynamodb:LeadingKeys': 'false'})
            self.assertEqual(statement['Condition']['StringEquals'], {'aws:RequestedRegion': 'ap-southeast-2'})
        self.assertEqual(cooking['Condition']['ForAllValues:StringEquals']['dynamodb:LeadingKeys'],
            sorted(self.scope.job_partitions + self.scope.source_partitions))
        self.assertEqual(data['Condition']['ForAllValues:StringEquals']['dynamodb:LeadingKeys'],
            sorted(self.scope.source_partitions))
        self.assertTrue(cooking['Resource'].endswith('/foodtech-cooking-jobs-dev'))
        self.assertTrue(data['Resource'].endswith('/foodtech-data-flow-dev'))
        self.assertNotIn('kms:', json.dumps(policy))
        self.assertNotIn('ReadDataForReplication', json.dumps(policy))

    def test_issuance_uses_opaque_session_no_tags_and_does_not_return_secret_strings(self):
        lease = self.broker.issue(self.scope)
        request = self.sts.assume_role.call_args.kwargs
        self.assertEqual(set(request), {'RoleArn', 'RoleSessionName', 'DurationSeconds', 'Policy'})
        self.assertEqual(request['RoleArn'], self.role)
        self.assertEqual(request['DurationSeconds'], 900)
        self.assertNotIn(self.scope.owner, request['RoleSessionName'])
        self.assertNotIn(self.scope.execution_id, request['RoleSessionName'])
        self.assertEqual(repr(lease), 'RecipeReadLease()')
        self.assertNotIn('synthetic-secret', repr(lease))
        with self.assertRaises(ValueError):
            jsonable_encoder(lease)
        with self.assertRaises(TypeError):
            pickle.dumps(lease)
        self.assertIs(lease.resource(self.scope), self.sessions.return_value.resource.return_value)
        with self.assertRaises(RecipeScopeDenied):
            lease.resource(replace(self.scope, owner='another_customer'))
        with self.assertRaises(RecipeScopeDenied):
            RecipeReadLease(self.scope, datetime.now(timezone.utc) - timedelta(seconds=1),
                self.sessions.return_value, self.broker.config).resource(self.scope)

    def test_bad_role_or_key_scope_never_calls_sts(self):
        bad = [replace(self.scope, owner='owner#injection'),
            replace(self.scope, job_partitions=(self.scope.job_partitions[0], self.scope.job_partitions[0])),
            replace(self.scope, job_partitions=('SCOPE#USER#foreign#JOB#' + str(uuid4()), self.scope.job_partitions[1])),
            replace(self.scope, job_partitions=('SCOPE#USER#opaque_subject#JOB#' + '-' * 36, self.scope.job_partitions[1])),
            replace(self.scope, source_partitions=('JEYUK#*', self.scope.source_partitions[1]))]
        for scope in bad:
            with self.subTest(scope=repr(scope)), self.assertRaises(RecipeScopeDenied):
                self.broker.issue(scope)
        self.sts.assume_role.assert_not_called()
        with self.assertRaises(ValueError):
            RecipeReadBroker(account='123456789012', stage='dev',
                read_role_arn=self.role.replace('-dev', '-demo'), sts=self.sts)

    def test_unexpected_sts_identity_or_expiration_never_constructs_session(self):
        for response in [
            {'AssumedRoleUser': {'Arn': 'arn:aws:sts::999999999999:assumed-role/foreign/test'}, 'Credentials': {}},
            {'AssumedRoleUser': {}, 'Credentials': {'Expiration': datetime.now()}},
        ]:
            self.sts.assume_role.side_effect = None
            self.sts.assume_role.return_value = response
            with self.assertRaises(RecipeScopeDenied):
                self.broker.issue(self.scope)
        self.sessions.assert_not_called()

    def test_disabled_or_unreviewed_runtime_never_discovers_credentials(self):
        for env in [{}, {'FOODTECH_RECIPE_SOURCE_BROKER_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE'}]:
            with patch.dict(os.environ, env, clear=True), patch(
                    'foodtech_business_api.recipe_read_broker.boto3.Session') as sessions:
                with self.assertRaises(RecipeScopeDenied):
                    configured_broker()
                sessions.assert_not_called()

    def test_configured_broker_rejects_non_task_credentials_and_wrong_actual_role(self):
        env = {'FOODTECH_RECIPE_SOURCE_BROKER_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
            'FOODTECH_STAGE': 'dev', 'FOODTECH_RECIPE_SOURCE_READ_ROLE_ARN': self.role,
            'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev', 'FOODTECH_SOURCE_ACCESS_TABLE': 'foodtech-source-access-dev',
            'FOODTECH_RECIPE_BROKER_TASK_ROLE_ARN': 'arn:aws:iam::123456789012:role/reviewed-business-task'}
        session = MagicMock()
        session.get_credentials.return_value = SimpleNamespace(method='env')
        with patch.dict(os.environ, env, clear=True), patch(
                'foodtech_business_api.recipe_read_broker.boto3.Session', return_value=session):
            with self.assertRaises(RecipeScopeDenied):
                configured_broker()
            session.client.assert_not_called()
            session.get_credentials.return_value = SimpleNamespace(method='container-role')
            session.client.return_value.get_caller_identity.return_value = {
                'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/gateway-task/test'}
            with self.assertRaises(RecipeScopeDenied):
                configured_broker()
            session.client.return_value.assume_role.assert_not_called()

    def test_configured_broker_binds_actual_issuer_and_guarded_sts_client(self):
        env = {'FOODTECH_RECIPE_SOURCE_BROKER_ENABLED': 'true', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
            'FOODTECH_STAGE': 'dev', 'FOODTECH_RECIPE_SOURCE_READ_ROLE_ARN': self.role,
            'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev', 'FOODTECH_SOURCE_ACCESS_TABLE': 'foodtech-source-access-dev',
            'FOODTECH_RECIPE_BROKER_TASK_ROLE_ARN': 'arn:aws:iam::123456789012:role/reviewed-business-task'}
        session = MagicMock()
        session.get_credentials.return_value = SimpleNamespace(method='container-role')
        session.client.return_value.get_caller_identity.return_value = {
            'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/reviewed-business-task/test'}
        with patch.dict(os.environ, env, clear=True), patch(
                'foodtech_business_api.recipe_read_broker.boto3.Session', return_value=session):
            broker = configured_broker()
        self.assertEqual(broker.read_role_arn, self.role)
        self.assertIs(broker.sts, session.client.return_value)
        self.assertTrue(session.client.call_args.kwargs['config'].ignore_configured_endpoint_urls)
        session.resource.assert_called_once_with('dynamodb', config=session.client.call_args.kwargs['config'],
                                                endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        self.assertIs(broker.cooking_resource, session.resource.return_value)
        session.resource.return_value.Table.assert_not_called()
        session.client.return_value.assume_role.assert_not_called()


class AuthenticatedRecipeReadTests(unittest.IsolatedAsyncioTestCase):
    def authority(self):
        return SimpleNamespace(authorize_read=MagicMock(return_value=True))

    async def test_revoke_during_issuance_discards_lease_before_any_dynamodb_read(self):
        fixture = RecipeReadBrokerTests()
        fixture.setUp()
        with patch('foodtech_business_api.recipe_read_broker.authenticated_recipe_scope',
                new=AsyncMock(return_value=fixture.scope)), patch(
                'foodtech_business_api.recipe_read_broker.revalidate_recipe_scope',
                new=AsyncMock(side_effect=RecipeScopeDenied('revoked'))):
            with self.assertRaises(RecipeScopeDenied):
                await issue_authenticated_reads(object(), fixture.scope.execution_id,
                    execution_reader=object(), job_reader=object(), broker=fixture.broker, source_authority=self.authority())
        fixture.sts.assume_role.assert_called_once()
        fixture.sessions.return_value.resource.assert_not_called()

    async def test_untrusted_execution_does_not_issue_and_valid_scope_is_rechecked(self):
        fixture = RecipeReadBrokerTests()
        fixture.setUp()
        with patch('foodtech_business_api.recipe_read_broker.authenticated_recipe_scope',
                new=AsyncMock(side_effect=RecipeScopeDenied('wrong owner'))):
            with self.assertRaises(RecipeScopeDenied):
                await issue_authenticated_reads(object(), fixture.scope.execution_id,
                    execution_reader=object(), job_reader=object(), broker=fixture.broker, source_authority=self.authority())
        fixture.sts.assume_role.assert_not_called()
        with patch('foodtech_business_api.recipe_read_broker.authenticated_recipe_scope',
                new=AsyncMock(return_value=fixture.scope)), patch(
                'foodtech_business_api.recipe_read_broker.revalidate_recipe_scope',
                new=AsyncMock()) as recheck:
            lease = await issue_authenticated_reads(object(), fixture.scope.execution_id,
                execution_reader=object(), job_reader=object(), broker=fixture.broker, source_authority=self.authority())
        self.assertIs(lease.scope, fixture.scope)
        recheck.assert_awaited_once()

    async def test_job_binding_alone_cannot_authorize_source_disclosure(self):
        fixture = RecipeReadBrokerTests()
        fixture.setUp()
        with patch('foodtech_business_api.recipe_read_broker.authenticated_recipe_scope',
                new=AsyncMock(return_value=fixture.scope)):
            for authority in [None, SimpleNamespace(authorize_read=lambda _: False)]:
                with self.assertRaises(RecipeScopeDenied):
                    await issue_authenticated_reads(object(), fixture.scope.execution_id,
                        execution_reader=object(), job_reader=object(), broker=fixture.broker, source_authority=authority)
        fixture.sts.assume_role.assert_not_called()

    async def test_source_authority_revoked_during_issue_discards_access(self):
        fixture = RecipeReadBrokerTests()
        fixture.setUp()
        authority = SimpleNamespace(authorize_read=MagicMock(side_effect=[True, False]))
        with patch('foodtech_business_api.recipe_read_broker.authenticated_recipe_scope',
                new=AsyncMock(return_value=fixture.scope)), patch(
                'foodtech_business_api.recipe_read_broker.revalidate_recipe_scope', new=AsyncMock()):
            with self.assertRaises(RecipeScopeDenied):
                await issue_authenticated_reads(object(), fixture.scope.execution_id,
                    execution_reader=object(), job_reader=object(), broker=fixture.broker, source_authority=authority)
        fixture.sessions.return_value.resource.assert_not_called()

    async def test_real_catalog_reader_is_connected_and_rechecks_after_assume_role(self):
        for mutation in ['none', 'suspend', 'version']:
            fixture = RecipeReadBrokerTests()
            fixture.setUp()
            refs = [str(uuid4()), str(uuid4())]
            principal = {'pk': 'USER#' + fixture.scope.owner, 'epoch': 0, 'suspended': False,
                'source_read_grants': dict(zip(fixture.scope.source_partitions, refs))}
            grants = [{'pk': 'GRANT#' + ref, 'sk': 'META', 'schema': 1, 'grant_id': ref,
                'source_partition': source, 'permission': 'RECIPE_SOURCE_READ', 'state': 'ACTIVE',
                'version': 1, 'expires_at': int(time.time()) + 300} for ref, source in zip(refs, fixture.scope.source_partitions)]
            serializer, client = TypeSerializer(), MagicMock()
            encode = lambda row: {key: serializer.serialize(value) for key, value in row.items()}
            client.get_item.side_effect = lambda **_: {'Item': encode(principal)}
            client.transact_get_items.side_effect = lambda **_: {'Responses': [
                {'Item': encode(principal)}, *({'Item': encode(row)} for row in grants)]}
            fixture.broker.source_authority = DynamoSourceReadAuthority(auth_table='foodtech-auth-sessions-dev',
                catalog_table='foodtech-source-access-dev', client=client)
            original = fixture.sts.assume_role.side_effect

            def assumed(**kwargs):
                response = original(**kwargs)
                if mutation == 'suspend':
                    principal['suspended'] = True
                elif mutation == 'version':
                    grants[0]['version'] += 1
                return response

            fixture.sts.assume_role.side_effect = assumed
            with self.subTest(mutation=mutation), patch(
                    'foodtech_business_api.recipe_read_broker.authenticated_recipe_scope',
                    new=AsyncMock(return_value=fixture.scope)), patch(
                    'foodtech_business_api.recipe_read_broker.revalidate_recipe_scope', new=AsyncMock()):
                call = issue_authenticated_reads(object(), fixture.scope.execution_id,
                    execution_reader=object(), job_reader=object(), broker=fixture.broker)
                if mutation != 'none':
                    with self.assertRaises(RecipeScopeDenied):
                        await call
                else:
                    lease = await call
                    self.assertEqual(lease.scope, fixture.scope)
                    self.assertEqual(lease.authorization.grant_ids, tuple(refs))
                    self.assertEqual(lease.authorization.grant_versions, (1, 1))
                    self.assertEqual(lease.authorization.auth_epoch, fixture.scope.auth_epoch)
            self.assertEqual(client.get_item.call_count, 2)
            fixture.sessions.return_value.resource.assert_not_called()
