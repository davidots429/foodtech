"""Mock HTTP/SDK integration; the real mTLS handshake is tested separately."""
import asyncio
import copy
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from foodtech_ai_gateway.main import app as gateway_app
from foodtech_business_api import recipe_scope
from foodtech_business_api.gateway_recipe_scope import GatewayRecipeExecutionReader, issue_customer_recipe_reads
from foodtech_business_api.recipe_read_broker import RecipeReadBroker
from foodtech_business_api.recipe_scope import RecipeScopeDenied
from foodtech_contracts.recipe_execution_scope import BINDING_FIELDS, parse_recipe_execution_observation


class GatewayRecipeScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner, self.execution_id = 'opaque_subject', str(uuid4())
        self.jobs = [str(uuid4()), str(uuid4())]
        self.execution = SimpleNamespace(execution_id=self.execution_id, tenant_id=self.owner,
            feature='RECIPE_CORRECTION', job_id=self.jobs[0], prior_job_id=self.jobs[1], job_version=2,
            prior_job_version=1, jeyuk_source_revision=12, prior_source_revision=11,
            prompt='synthetic private prompt', credential_id='synthetic private credential')
        self.snapshot = SimpleNamespace(execution=self.execution, state='PENDING', version=1)
        self.registry = SimpleNamespace(execution_snapshot=MagicMock(side_effect=lambda _: copy.deepcopy(self.snapshot)))
        gateway_app.state.gateway_service = SimpleNamespace(registry=self.registry)
        gateway_app.state.mtls_listener = True
        self.principal = {'epoch': 0, 'suspended': False}
        self.session = SimpleNamespace(subject=self.owner, surface='customer', csrf='test-csrf',
                                       epoch=0, expires=int(time.time()) + 300)
        self.requests = []

        def handler(request):
            self.requests.append(request)
            async def call():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app),
                        base_url='https://127.0.0.1') as client:
                    result = await client.post(request.url.path, content=request.content,
                                               headers={'Content-Type': 'application/json'})
                    return httpx.Response(result.status_code, headers=result.headers, content=result.content)
            return asyncio.run(call())

        self.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.reader = GatewayRecipeExecutionReader(client=self.client, url='https://127.0.0.1', subject=self.owner)
        self.job_rows = {f'SCOPE#USER#{self.owner}#JOB#{job}': {'job_id': job, 'owner_subject': self.owner,
            'version': version, 'menu': 'JEYUK', 'provenance': 'SYNTHETIC', 'source_binding': {
                'source_id': 'synthetic-rest', 'device_id': 'mockprofile-110-a-001', 'batch_id': batch}}
            for job, version, batch in zip(self.jobs, (2, 1), ('jeyuk-current', 'jeyuk-prior'))}

    def tearDown(self):
        self.client.close()
        del gateway_app.state.gateway_service
        del gateway_app.state.mtls_listener

    async def test_private_endpoint_returns_only_server_binding_and_no_store(self):
        result = await asyncio.to_thread(self.reader, self.execution_id)
        self.assertEqual(result.execution.tenant_id, self.owner)
        request = self.requests[0]
        self.assertEqual(json.loads(request.content), {'tenant_id': self.owner})
        self.assertNotIn('cookie', request.headers)
        self.assertNotIn('authorization', request.headers)
        self.assertNotIn(self.owner, repr(result))
        self.assertNotIn('prompt', repr(result))
        self.registry.execution_snapshot.assert_called_once_with(self.execution_id)

    async def test_gateway_rejects_foreign_missing_terminal_incomplete_and_no_mtls(self):
        variants = [None, SimpleNamespace(execution=self.execution, state='SUCCEEDED', version=1),
                    SimpleNamespace(execution=self.execution, state='PENDING', version=True)]
        for snapshot in variants:
            self.snapshot = snapshot
            with self.assertRaises(RecipeScopeDenied):
                await asyncio.to_thread(self.reader, self.execution_id)
        self.snapshot = SimpleNamespace(execution=self.execution, state='PENDING', version=1)
        self.execution.tenant_id = 'foreign'
        with self.assertRaises(RecipeScopeDenied):
            await asyncio.to_thread(self.reader, self.execution_id)
        gateway_app.state.mtls_listener = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url='http://127.0.0.1') as client:
            result = await client.post(f'/internal/ai/recipe-scopes/{self.execution_id}', json={'tenant_id': self.owner})
            self.assertEqual(result.status_code, 401)
        self.execution.tenant_id = self.owner
        gateway_app.state.mtls_listener = False
        with self.assertRaises(RecipeScopeDenied):
            await asyncio.to_thread(self.reader, self.execution_id)

    async def test_two_gateway_observations_detect_changed_execution_before_any_issue(self):
        calls = 0
        def snapshot(_):
            nonlocal calls
            calls += 1
            result = copy.deepcopy(self.snapshot)
            if calls > 1:
                result.version += 1
            return result
        self.registry.execution_snapshot.side_effect = snapshot
        with patch.object(recipe_scope, '_session', new=AsyncMock(return_value=self.session)), \
             patch.object(recipe_scope, 'get_store', return_value=SimpleNamespace(principal_record=lambda _: self.principal)):
            with self.assertRaises(RecipeScopeDenied):
                await recipe_scope.authenticated_recipe_scope(object(), self.execution_id, self.reader, self.job_rows.get)
        self.assertEqual(calls, 2)

    async def test_customer_csrf_gateway_observation_and_sts_are_connected_without_exporting_lease(self):
        sts, sessions = MagicMock(), MagicMock()
        def assume(**kwargs):
            return {'AssumedRoleUser': {'Arn': 'arn:aws:sts::123456789012:assumed-role/'
                'foodtech-recipe-source-read-dev/' + kwargs['RoleSessionName']}, 'Credentials': {
                'AccessKeyId': 'synthetic', 'SecretAccessKey': 'synthetic', 'SessionToken': 'synthetic',
                'Expiration': datetime.now(timezone.utc) + timedelta(seconds=899)}}
        sts.assume_role.side_effect = assume
        broker = RecipeReadBroker(account='123456789012', stage='dev',
            read_role_arn='arn:aws:iam::123456789012:role/foodtech-recipe-source-read-dev', sts=sts,
            session_factory=sessions, source_authority=SimpleNamespace(authorize_read=lambda _: True))
        request = Request({'type': 'http', 'headers': [(b'x-csrf-token', b'test-csrf')], 'method': 'POST'})
        context = MagicMock()
        with patch('foodtech_business_api.gateway_recipe_scope._session', new=AsyncMock(return_value=self.session)), \
             patch.object(recipe_scope, '_session', new=AsyncMock(return_value=self.session)), \
             patch.object(recipe_scope, 'get_store', return_value=SimpleNamespace(principal_record=lambda _: self.principal)), \
             patch('foodtech_business_api.ai._gateway_settings', return_value=('https://127.0.0.1', 'ca', 'cert', 'key')), \
             patch('ssl.create_default_context', return_value=context), \
             patch('foodtech_business_api.gateway_recipe_scope.httpx.Client', return_value=self.client):
            lease = await issue_customer_recipe_reads(request, self.execution_id, job_reader=self.job_rows.get, broker=broker)
        self.assertEqual(lease.scope.execution_id, self.execution_id)
        self.assertEqual(len(self.requests), 4)
        sts.assume_role.assert_called_once()
        sessions.return_value.resource.assert_not_called()
        context.load_cert_chain.assert_called_once_with(certfile='cert', keyfile='key')

    async def test_malformed_extra_duplicate_oversized_or_redirected_metadata_is_denied(self):
        valid = {key: getattr(self.execution, key) for key in BINDING_FIELDS}
        valid.update(state='PENDING', version=1)
        bodies = [json.dumps({**valid, 'credential': 'synthetic'}),
                  json.dumps(valid).replace('"PENDING"', '[]'), '{"version":1,"version":2}', 'x' * 4097]
        for body in bodies:
            with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
                    headers={'Cache-Control': 'no-store'}, content=body))) as client:
                reader = GatewayRecipeExecutionReader(client=client, url='https://localhost', subject=self.owner)
                with self.assertRaises(RecipeScopeDenied):
                    reader(self.execution_id)
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(307,
                headers={'Location': 'https://external.invalid', 'Cache-Control': 'no-store'}))) as client:
            with self.assertRaises(RecipeScopeDenied):
                GatewayRecipeExecutionReader(client=client, url='https://localhost', subject=self.owner)(self.execution_id)
        with self.assertRaises(ValueError):
            parse_recipe_execution_observation(valid, execution_id=self.execution_id, tenant_id='foreign')

    async def test_missing_csrf_stops_before_gateway_configuration_or_issuance(self):
        request = Request({'type': 'http', 'headers': [], 'method': 'POST'})
        broker = MagicMock()
        with patch('foodtech_business_api.gateway_recipe_scope._session', new=AsyncMock(return_value=self.session)), \
             patch('foodtech_business_api.ai._gateway_settings') as settings:
            with self.assertRaises(HTTPException) as failure:
                await issue_customer_recipe_reads(request, self.execution_id, job_reader=self.job_rows.get, broker=broker)
        self.assertEqual(failure.exception.status_code, 403)
        settings.assert_not_called()
        broker.issue.assert_not_called()
