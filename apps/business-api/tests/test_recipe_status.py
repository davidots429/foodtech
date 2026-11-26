"""BFF -> mocked mTLS Gateway integration, with no model or AWS calls."""
import copy
import json
import os
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx

from foodtech_ai_gateway.main import app as gateway_app
from foodtech_ai_gateway.policy import Execution, ExecutionSnapshot, Feature
from foodtech_business_api import ai, cooking
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class RecipeStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.subject, self.epoch = 'customer_a', 0
        self.job_id, self.execution_id = str(uuid4()), str(uuid4())
        self.store = MemoryCookingStore()
        self.pk = cooking._pk(self.subject, self.job_id)
        self.store.items[(self.pk, 'META')] = {'job_id': self.job_id, 'version': 3}
        self.execution = Execution(self.execution_id, self.subject, Feature.RECIPE_CORRECTION, 'model', None,
            'SYNTHETIC', Decimal(0), job_id=self.job_id, job_version=3)
        self.state, self.version, self.result = 'PENDING', 1, None
        self.requests = []
        self.on_response = None
        self.registry = SimpleNamespace(execution_snapshot=MagicMock(side_effect=self.snapshot),
            execution_status_snapshot=MagicMock(side_effect=lambda *_: (self.snapshot(), self.result)),
            create_execution=MagicMock(side_effect=AssertionError('status must not create an execution')))
        gateway_app.state.gateway_service = SimpleNamespace(registry=self.registry)
        gateway_app.state.mtls_listener = True

        async def session(*_):
            return SimpleNamespace(subject=self.subject, epoch=self.epoch, csrf='csrf')

        async def handler(request):
            self.requests.append(request)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url='https://gateway') as client:
                result = await client.post(request.url.path, content=request.content,
                                           headers={'Content-Type': 'application/json'})
            if self.on_response:
                self.on_response()
            return httpx.Response(result.status_code, headers=result.headers, content=result.content)

        self.patches = [patch.dict(os.environ, {'FOODTECH_SECURE_COOKIES': 'false'}),
            patch.object(cooking, '_session', side_effect=session),
            patch.object(cooking, 'get_cooking_store', return_value=self.store),
            patch.object(ai, '_gateway_settings', return_value=('https://gateway', 'ca', 'cert', 'key')),
            patch.object(ai, '_client', side_effect=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(handler)))]
        for patcher in self.patches:
            patcher.start()

    def snapshot(self, *_):
        return ExecutionSnapshot(copy.deepcopy(self.execution), self.state, self.version)

    async def asyncTearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        del gateway_app.state.gateway_service
        del gateway_app.state.mtls_listener

    async def get(self, **params):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.get(f'/api/customer/ai/recipe-executions/{self.execution_id}/status',
                                    params={'job_id': self.job_id, **params})

    async def test_owner_bound_status_has_no_model_call_and_is_not_cached(self):
        result = await self.get(tenant_id='forged')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        self.assertEqual(result.json(), {'execution_id': self.execution_id, 'job_id': self.job_id,
            'job_version': 3, 'state': 'PENDING', 'result_available': False, 'execution_available': False, 'job_changed': False})
        self.assertNotIn('tenant_id=', str(self.requests[0].url))
        self.assertEqual(json.loads(self.requests[0].content)['tenant_id'], 'customer_a')
        self.registry.create_execution.assert_not_called()

    async def test_terminal_record_never_returns_response_text_secret_or_error_body(self):
        self.state = 'SUCCEEDED'
        self.result = {'state': 'SUCCEEDED', 'response_text': 'synthetic-secret-marker', 'credential': 'synthetic-key'}
        result = await self.get()
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['result_available'])
        self.assertNotIn('synthetic-secret-marker', result.text)
        self.assertNotIn('credential', result.text)
        self.assertNotIn('response_text', result.json())

    async def test_foreign_owner_wrong_job_or_other_feature_is_rejected_before_result_read(self):
        from dataclasses import replace
        original = self.execution
        for execution in [replace(original, tenant_id='foreign'), replace(original, job_id=str(uuid4())),
                          replace(original, feature=Feature.GENERAL_GUIDE)]:
            self.execution = execution
            result = await self.get()
            self.assertEqual(result.status_code, 404)
        self.registry.execution_status_snapshot.assert_not_called()
        self.subject = 'another_customer'
        self.assertEqual((await self.get()).status_code, 404)

    async def test_job_change_is_reported_but_session_change_prevents_return(self):
        self.on_response = lambda: self.store.items[(self.pk, 'META')].update(version=4)
        self.assertTrue((await self.get()).json()['job_changed'])
        self.on_response = lambda: setattr(self, 'epoch', 1)
        self.assertEqual((await self.get()).status_code, 401)

    async def test_changed_or_inconsistent_execution_is_not_reported_as_stable(self):
        self.registry.execution_status_snapshot.side_effect = lambda *_: (
            ExecutionSnapshot(self.execution, 'PENDING', self.version + 1), None)
        self.assertEqual((await self.get()).status_code, 409)
        self.registry.execution_status_snapshot.side_effect = lambda *_: (self.snapshot(), {'state': 'FAILED'})
        self.assertEqual((await self.get()).status_code, 409)

    async def test_mtls_marker_is_required_and_invalid_ids_never_get_forwarded(self):
        gateway_app.state.mtls_listener = False
        self.assertEqual((await self.get()).status_code, 502)
        count = len(self.requests)
        self.execution_id = 'not-a-valid-execution'
        self.assertEqual((await self.get()).status_code, 422)
        self.assertEqual(len(self.requests), count)
