"""Opt-in invocation boundaries, verified SDK identity and concurrent tenant isolation."""
import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
import httpx

from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.policy import Feature
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService, ExecutionOutcome
from foodtech_ai_gateway.recipe_invocation import (
    RecipeRuntimeUnavailable, configured_recipe_conditions, invoke_observed_recipe,
)


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.env = {'FOODTECH_STAGE': 'dev', 'AWS_EXECUTION_ENV': 'AWS_ECS_FARGATE',
            'FOODTECH_RECIPE_INVOCATION_ENABLED': 'true', 'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true',
            'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev',
            'FOODTECH_RECIPE_GATEWAY_TASK_ROLE_ARN': 'arn:aws:iam::123456789012:role/gateway-dev',
            'FOODTECH_RECIPE_SOURCE_READ_ROLE_ARN': 'arn:aws:iam::123456789012:role/foodtech-recipe-source-read-dev'}
        with patch.dict(os.environ, self.env):
            self.registry = DynamoRegistry('foodtech-ai-execution-registry-dev', '2026-11',
                resource=MagicMock(), client=MagicMock(), auth_table='foodtech-auth-sessions-dev')
        self.session = MagicMock()
        self.session.get_credentials.return_value = SimpleNamespace(method='container-role')
        self.sts, self.conditions, self.resource = MagicMock(), MagicMock(), MagicMock()
        self.sts.get_caller_identity.return_value = {'Account': '123456789012',
            'Arn': 'arn:aws:sts::123456789012:assumed-role/gateway-dev/session-1'}
        self.session.client.side_effect = lambda service, **kw: self.sts if service == 'sts' else self.conditions
        self.session.resource.return_value = self.resource

    def test_verified_task_session_is_injected_into_registry_and_condition_checks(self):
        with patch.dict(os.environ, self.env), patch('foodtech_ai_gateway.recipe_invocation.boto3.Session', return_value=self.session):
            role, client, registry = configured_recipe_conditions(self.registry)
        self.assertEqual(role, self.env['FOODTECH_RECIPE_SOURCE_READ_ROLE_ARN'])
        self.assertIs(client, self.conditions)
        self.assertIs(registry.client, client)
        self.assertIs(registry.resource, self.resource)
        self.assertIsNot(registry, self.registry)
        self.assertTrue(registry.require_principal)
        self.sts.close.assert_called_once()
        for call in self.session.client.call_args_list:
            config = call.kwargs['config']
            self.assertTrue(config.ignore_configured_endpoint_urls)
            self.assertEqual(config.proxies, {})
            self.assertEqual(config.retries['total_max_attempts'], 2)
            self.assertEqual(call.kwargs['endpoint_url'], f'https://{call.args[0]}.ap-southeast-2.amazonaws.com')

    def test_disabled_wrong_stage_role_account_or_registry_rejected_before_sdk(self):
        changes = [{'FOODTECH_RECIPE_INVOCATION_ENABLED': 'false'},
            {'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'false'}, {'AWS_EXECUTION_ENV': ''},
            {'FOODTECH_STAGE': 'other'}, {'FOODTECH_AUTH_TABLE': 'other'},
            {'FOODTECH_RECIPE_GATEWAY_TASK_ROLE_ARN': self.env['FOODTECH_RECIPE_SOURCE_READ_ROLE_ARN']},
            {'FOODTECH_RECIPE_SOURCE_READ_ROLE_ARN': 'arn:aws:iam::999999999999:role/foodtech-recipe-source-read-dev'}]
        for change in changes:
            with self.subTest(change=change), patch.dict(os.environ, {**self.env, **change}), \
                    patch('foodtech_ai_gateway.recipe_invocation.boto3.Session') as sdk:
                with self.assertRaises(RecipeRuntimeUnavailable):
                    configured_recipe_conditions(self.registry)
                sdk.assert_not_called()

    def test_developer_credentials_and_other_actual_gateway_identity_rejected(self):
        for method, account, arn in [('env', '123456789012', 'irrelevant'),
                ('container-role', '999999999999', 'arn:aws:sts::999999999999:assumed-role/gateway-dev/session-1'),
                ('container-role', '123456789012', 'arn:aws:sts::123456789012:assumed-role/other/session-1')]:
            self.session.get_credentials.return_value = SimpleNamespace(method=method)
            self.sts.get_caller_identity.return_value = {'Account': account, 'Arn': arn}
            self.session.client.reset_mock()
            self.session.resource.reset_mock()
            with self.subTest(method=method, arn=arn), patch.dict(os.environ, self.env), \
                    patch('foodtech_ai_gateway.recipe_invocation.boto3.Session', return_value=self.session):
                with self.assertRaises(RecipeRuntimeUnavailable):
                    configured_recipe_conditions(self.registry)
                self.assertNotIn('dynamodb', [call.args[0] for call in self.session.client.call_args_list])
                self.session.resource.assert_not_called()


class InvocationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_http_and_disabled_route_stop_before_service_or_aws(self):
        for scheme, listener, enabled, expected in [('http', False, 'true', 401),
                ('https', False, 'true', 401), ('https', True, 'false', 503)]:
            absent=object()
            previous=getattr(app.state,'mtls_listener',absent)
            app.state.mtls_listener = listener
            try:
                with patch.dict(os.environ, {'FOODTECH_RECIPE_INVOCATION_ENABLED': enabled}), \
                        patch('foodtech_ai_gateway.main._service') as service:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=scheme+'://test') as client:
                        response = await client.post('/internal/ai/recipe-executions/'+str(uuid4())+'/invoke', content=b'synthetic-secret-marker')
                    self.assertEqual(response.status_code, expected)
                    self.assertNotIn('synthetic-secret-marker', response.text)
                    service.assert_not_called()
            finally:
                if previous is absent: del app.state.mtls_listener
                else: app.state.mtls_listener=previous

    async def test_changed_execution_never_calls_model_or_shared_service(self):
        identifier = str(uuid4())
        snapshot = SimpleNamespace(execution=SimpleNamespace(execution_id=identifier, tenant_id='one', feature=Feature.RECIPE_CORRECTION))
        registry = MagicMock()
        registry.execution_snapshot.side_effect = [snapshot, None]
        client = MagicMock()
        shared = GatewayService(registry)
        with patch.dict(os.environ, {'FOODTECH_RECIPE_INVOCATION_ENABLED':'true'}), \
                patch('foodtech_ai_gateway.recipe_invocation.configured_recipe_conditions', return_value=('read-role', client, registry)), \
                patch('foodtech_ai_gateway.recipe_invocation.receive_internal_recipe_handoff', new=AsyncMock(return_value=object())), \
                patch.object(GatewayService, 'execute_observed_local_recipe', new=AsyncMock()) as execute:
            with self.assertRaises(HTTPException) as error:
                await invoke_observed_recipe(object(), identifier, service=shared, ollama=object())
            self.assertEqual(error.exception.status_code, 409)
            execute.assert_not_awaited()
        client.close.assert_called_once()
        registry.resource.meta.client.close.assert_called_once()
        self.assertIsNone(shared.recipe_source_guard)

    async def test_concurrent_invocations_keep_guards_and_registries_request_scoped(self):
        identifiers = [str(uuid4()), str(uuid4())]
        shared = GatewayService(object())
        runtimes, guards, observed = {}, {}, []
        for index, identifier in enumerate(identifiers):
            snapshot = SimpleNamespace(execution=SimpleNamespace(execution_id=identifier, tenant_id=str(index), feature=Feature.RECIPE_CORRECTION))
            registry = MagicMock()
            registry.execution_snapshot.return_value = snapshot
            runtimes[identifier] = ('read-role', MagicMock(), registry)
            guards[identifier] = object()
        snapshots = {key: runtimes[key][2].execution_snapshot.return_value for key in identifiers}
        for _, _, registry in runtimes.values():
            registry.execution_snapshot.side_effect = lambda identifier: snapshots[identifier]
        calls = iter([runtimes[key] for key in identifiers])
        async def receive(request, **kwargs):
            return guards[request]
        barrier = asyncio.Event()
        async def execute(scoped, caller, identifier, *, ollama):
            observed.append((scoped.recipe_source_guard, scoped.registry, identifier))
            if len(observed) == 2: barrier.set()
            await asyncio.wait_for(barrier.wait(), 2)
            self.assertIs(scoped.recipe_source_guard, guards[identifier])
            self.assertIn(scoped.registry, [item[2] for item in runtimes.values()])
            return ExecutionOutcome(identifier, 'DENIED', error_code='HISTORY_SCOPE_UNAVAILABLE')
        with patch.dict(os.environ, {'FOODTECH_RECIPE_INVOCATION_ENABLED':'true'}), \
                patch('foodtech_ai_gateway.recipe_invocation.configured_recipe_conditions', side_effect=lambda _: next(calls)), \
                patch('foodtech_ai_gateway.recipe_invocation.receive_internal_recipe_handoff', new=receive), \
                patch.object(GatewayService, 'execute_observed_local_recipe', new=execute):
            responses = await asyncio.gather(*[invoke_observed_recipe(key, key, service=shared, ollama=object()) for key in identifiers])
        self.assertEqual(len({id(item[1]) for item in observed}), 2)
        self.assertTrue(all(response.headers['cache-control']=='no-store' for response in responses))
        self.assertIsNone(shared.recipe_source_guard)

    async def test_actual_internal_route_with_v1_handoff_denies_before_model(self):
        # Reuse the existing synthetic handoff fixture; parse/authorize the real
        # wire format here and run the real service's mandatory history check.
        from test_recipe_read_handoff import RecipeReadHandoffTests
        from foodtech_ai_gateway.recipe_read_handoff import receive_internal_recipe_handoff
        from foodtech_contracts.recipe_read_handoff import CONTENT_TYPE
        fixture=RecipeReadHandoffTests();fixture.setUp()
        registry=MagicMock()
        registry.execution_snapshot.return_value=fixture.snapshot
        registry.execution.return_value=fixture.snapshot.execution
        shared=GatewayService(registry)
        conditions=fixture.conditions
        ollama=MagicMock()
        async def receive(request, **kwargs):
            return await receive_internal_recipe_handoff(request, **kwargs, session_factory=fixture.sessions)
        with patch.dict(os.environ,{'FOODTECH_RECIPE_INVOCATION_ENABLED':'true'}), \
                patch.object(app.state,'mtls_listener',True,create=True), \
                patch.object(app.state,'ollama_client',ollama,create=True), \
                patch('foodtech_ai_gateway.main._service',return_value=shared), \
                patch('foodtech_ai_gateway.recipe_invocation.configured_recipe_conditions',return_value=(fixture.role,conditions,registry)), \
                patch('foodtech_ai_gateway.recipe_invocation.receive_internal_recipe_handoff',new=receive):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
                response=await client.post('/internal/ai/recipe-executions/'+fixture.execution_id+'/invoke',
                    content=fixture.raw,headers={'Content-Type':CONTENT_TYPE})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['state'],'DENIED')
        self.assertEqual(response.json()['error_code'],'HISTORY_SCOPE_UNAVAILABLE')
        self.assertEqual([call for call in ollama.mock_calls if call[0] != '__bool__'],[])
        self.assertNotIn('synthetic-secret-marker',response.text)
        self.assertIsNone(shared.recipe_source_guard)
