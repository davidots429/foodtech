from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import Mock, patch
import time
from uuid import uuid4

import httpx
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_contracts.combi_execution_binding import CombiExecutionBinding, parse_combi_binding, combi_binding_payload
from foodtech_ai_gateway.downgrade_flow import DowngradeError, DowngradeFlow, NOTICE_VERSION
from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.policy import Caller, Execution, Feature, Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry, RegistryConflict
from foodtech_ai_gateway.service import GatewayService
from foodtech_contracts.recommendation_output import TonkatsuBaseline
from foodtech_contracts.recommendation_guard import Evidence
from test_registry import AtomicClient, MemoryResource
from test_combi_observed_inputs import fixture


def binding():
    return CombiExecutionBinding('RICE', str(uuid4()), 12, str(uuid4()), 'a' * 64,
                                str(uuid4()), 10, str(uuid4()), 'b' * 64, 0)


class CombiExecutionBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.resource = MemoryResource()
        self.client = AtomicClient(self.resource.table)
        self.client.auth_states['USER#customer-a'] = {'epoch': 0}
        self.registry = DynamoRegistry('registry-test', '2026-11', resource=self.resource, client=self.client,
                                      auth_table='auth-test', local_models={'llama3.1:8b'})
        now = datetime.now(timezone.utc)
        self.registry.create_model(Model('llama3.1:8b', 'ollama', Tier.TIER_3, 'ACTIVE', True, True, now, now))
        self.registry.create_model(Model('tier-one', 'ollama', Tier.TIER_1, 'INACTIVE', True, True, now, now))
        self.registry.create_limit('TENANT', 'customer-a', Decimal(0), calls_remaining=3)
        self.registry.create_limit('PROVIDER', 'ollama', Decimal(0))
        self.registry.create_limit('PROJECT', 'GLOBAL', Decimal(0))
        self.binding = binding()
        self.parent = Execution(str(uuid4()), 'customer-a', Feature.RECIPE_CORRECTION, 'tier-one', None,
            'SYNTHETIC', Decimal(0), job_id=self.binding.current_job_id, job_version=12,
            prior_job_id=self.binding.prior_job_id, prior_job_version=10, combi_binding=self.binding)
        self.registry.create_execution(self.parent)
        self.flow = DowngradeFlow(self.registry)

    def test_persisted_roundtrip_and_legacy_records(self):
        self.assertEqual(self.registry.execution(self.parent.execution_id), self.parent)
        raw = self.resource.table.items[('EXEC#' + self.parent.execution_id, 'META')]
        raw['combi_binding']['current_meal_version'] = Decimal(12)
        raw['combi_binding']['auth_epoch'] = Decimal(0)
        self.assertEqual(self.registry.execution(self.parent.execution_id), self.parent)
        legacy = replace(self.parent, execution_id=str(uuid4()), combi_binding=None)
        self.registry.create_execution(legacy)
        del self.resource.table.items[('EXEC#' + legacy.execution_id, 'META')]['combi_binding']
        self.assertIsNone(self.registry.execution(legacy.execution_id).combi_binding)

    def test_invalid_unknown_mixed_or_unbounded_binding_rejected(self):
        original = combi_binding_payload(self.binding)
        for changes in ({'auth_epoch': True}, {'auth_epoch': Decimal('1E100000')}, {'menu': 'JEYUK'},
                        {'current_meal_version': True}, {'current_meal_version': Decimal('1E100000')},
                        {'prior_meal_id': self.binding.current_meal_id}, {'prior_source_sha256': 'a' * 64},
                        {'extra': 'not-allowed'}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): parse_combi_binding({**original, **changes})
        for changes in ({'job_version': True}, {'job_id': str(uuid4())}, {'jeyuk_source_revision': 1},
                        {'comparison_id': 'c' * 64}, {'feature': Feature.GENERAL_GUIDE}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): replace(self.parent, **changes)

    def test_notice_consent_and_local_execution_keep_the_identical_binding(self):
        quote = self.flow.preview('customer-a', self.parent.execution_id)
        self.assertEqual(parse_combi_binding(quote['combi_binding']), self.binding)
        local = self.flow.confirm('customer-a', quote['quote_id'], NOTICE_VERSION, True)
        self.assertEqual(self.registry.execution(local).combi_binding, self.binding)
        self.assertEqual(self.registry.consent(quote['quote_id']).execution_id, local)
        check = self.client.calls[-1][0]['ConditionCheck']
        self.assertIn('#epoch = :expected_epoch', check['ConditionExpression'])
        self.assertEqual(check['ReturnValuesOnConditionCheckFailure'], 'NONE')
        shape = get_session().get_service_model('dynamodb').operation_model('TransactWriteItems').input_shape
        validate_parameters({'TransactItems': self.client.calls[-1], 'ClientRequestToken': str(uuid4())}, shape)

    def test_changed_or_malformed_notice_binding_never_creates_execution_or_consent(self):
        for change in ({'menu': 'MACKEREL'}, {'auth_epoch': True}, {'prior_source_sha256': 'c' * 64}):
            with self.subTest(change=change):
                quote = self.flow.preview('customer-a', self.parent.execution_id)
                raw = self.resource.table.items[('QUOTE#' + quote['quote_id'], 'META')]
                raw['combi_binding'].update(change)
                with self.assertRaises(DowngradeError):
                    self.flow.confirm('customer-a', quote['quote_id'], NOTICE_VERSION, True)
                self.assertIsNone(self.registry.execution(quote['execution_id']))
                self.assertIsNone(self.registry.consent(quote['quote_id']))

    def test_auth_epoch_change_before_creation_or_confirmation_is_atomic(self):
        before = set(self.resource.table.items)
        new_parent = replace(self.parent, execution_id=str(uuid4()))
        self.client.before_commit = lambda: self.client.auth_states['USER#customer-a'].update(epoch=1)
        with self.assertRaises(RegistryConflict): self.registry.create_execution(new_parent)
        self.assertEqual(set(self.resource.table.items), before)
        with self.assertRaises(RegistryConflict): self.flow.preview('customer-a', self.parent.execution_id)
        self.assertEqual(set(self.resource.table.items), before)
        self.client.auth_states['USER#customer-a']['epoch'] = 0
        quote = self.flow.preview('customer-a', self.parent.execution_id)
        self.client.before_commit = lambda: self.client.auth_states['USER#customer-a'].update(epoch=1)
        with self.assertRaises(DowngradeError): self.flow.confirm('customer-a', quote['quote_id'], NOTICE_VERSION, True)
        self.assertEqual(self.registry.quote(quote['quote_id'])['state'], 'OPEN')
        self.assertIsNone(self.registry.execution(quote['execution_id']))
        self.assertIsNone(self.registry.consent(quote['quote_id']))

    async def test_new_binding_cannot_invoke_using_a_legacy_baseline(self):
        quote = self.flow.preview('customer-a', self.parent.execution_id)
        local = self.flow.confirm('customer-a', quote['quote_id'], NOTICE_VERSION, True)
        provider = AsyncMock()
        result = await GatewayService(self.registry).execute_local_recipe(Caller('business-api', 'customer-a'), local,
            evidence=Evidence('COMPLETE', 'PASSED', True, False), baseline=TonkatsuBaseline('STANDARD', 300),
            context={}, ollama=provider)
        self.assertEqual(result.error_code, 'COMBI_SOURCE_SCOPE_UNAVAILABLE')
        provider.generate.assert_not_called()
        self.assertEqual(self.registry.execution_state(local), 'PENDING')

    async def test_internal_request_rejects_mixed_binding_before_storage(self):
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry)
        try:
            body = {'tenant_id': 'customer-a', 'job_id': self.parent.job_id, 'job_version': 12,
                'prior_job_id': self.parent.prior_job_id, 'prior_job_version': 10,
                'combi_binding': combi_binding_payload(self.binding)}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gateway') as client:
                good = await client.post('/internal/ai/recipe-requests', json=body)
                self.assertEqual(good.status_code, 201, good.text)
                self.assertFalse(good.json()['execution_available'])
                self.assertEqual(self.registry.execution(good.json()['execution_id']).combi_binding, self.binding)
                before = set(self.resource.table.items)
                for changed in ({'jeyuk_source_revision': 1}, {'prior_job_version': 9},
                                {'combi_binding': {**body['combi_binding'], 'auth_epoch': True}}):
                    bad = await client.post('/internal/ai/recipe-requests', json={**body, **changed})
                    self.assertEqual(bad.status_code, 422, bad.text)
                self.assertEqual(set(self.resource.table.items), before)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service

    async def test_internal_request_notice_and_confirmation_preserve_combi_scope(self):
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry)
        try:
            body = {'tenant_id': 'customer-a', 'job_id': self.parent.job_id, 'job_version': 12,
                'prior_job_id': self.parent.prior_job_id, 'prior_job_version': 10,
                'combi_binding': combi_binding_payload(self.binding)}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gateway') as client:
                created = await client.post('/internal/ai/recipe-requests', json=body)
                self.assertEqual(created.status_code, 201, created.text)
                preview = await client.post('/internal/ai/recipe-downgrades/preview',
                    json={**body, 'parent_execution_id': created.json()['execution_id']})
                self.assertEqual(preview.status_code, 201, preview.text)
                quote = preview.json()
                confirmed_body = {**body, 'notice_version': quote['notice_version'], 'quality_warning_acknowledged': True}
                changed = {**confirmed_body, 'combi_binding': {**body['combi_binding'], 'menu': 'MACKEREL'}}
                path = '/internal/ai/recipe-downgrades/' + quote['quote_id'] + '/confirm'
                self.assertEqual((await client.post(path, json=changed)).status_code, 409)
                confirmed = await client.post(path, json=confirmed_body)
                self.assertEqual(confirmed.status_code, 201, confirmed.text)
                self.assertEqual(self.registry.execution(confirmed.json()['execution_id']).combi_binding, self.binding)
                self.assertEqual((await client.post(path, json=confirmed_body)).status_code, 409)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service

    async def test_business_to_gateway_flow_reads_real_stored_observations_and_never_calls_provider(self):
        # Consumer record fixtures, not a Python protocol Mock implementation.
        from foodtech_business_api import cooking, combi_ai
        from foodtech_business_api.cooking_store import MemoryCookingStore
        from foodtech_business_api.main import app as business_app
        store = MemoryCookingStore()
        meals = []
        for label in ('current', 'prior'):
            rows, raw = fixture(label)
            meal = rows['META']['meal_batch_id']
            meals.append(meal)
            pk = f'SCOPE#USER#customer-a#COMBI_MEAL#{meal}'
            for sk, row in rows.items(): store.items[(pk, sk)] = {**deepcopy(row), 'pk': pk}
            for offset in range(0, len(raw), 100):
                sk = f'RAW#{offset // 100:04d}'
                store.items[(pk + '#SOURCE', sk)] = {'pk': pk + '#SOURCE', 'sk': sk,
                    'first_sequence': offset + 1, 'records': raw[offset:offset+100]}
        session = SimpleNamespace(subject='customer-a', epoch=0, surface='customer', csrf='csrf',
                                  expires=int(time.time())+300)
        auth_store = SimpleNamespace(principal_record=Mock(return_value={'pk': 'USER#customer-a', 'epoch': 0}))
        provider = AsyncMock()
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry, provider=provider)
        async def forward(path, payload):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gateway') as gateway:
                response = await gateway.post(path, json=payload)
                self.assertEqual(response.status_code, 201, response.text)
                return response.json()
        try:
            with patch.dict('os.environ', {'FOODTECH_SECURE_COOKIES': 'false'}), \
                 patch.object(cooking, '_session', AsyncMock(return_value=session)), \
                 patch.object(combi_ai, '_session', AsyncMock(return_value=session)), \
                 patch.object(cooking, 'get_cooking_store', return_value=store), \
                 patch.object(combi_ai, 'get_store', return_value=auth_store), \
                 patch('foodtech_business_api.recipe_scope.get_store', return_value=auth_store), \
                 patch.object(combi_ai, '_recipe_gateway', side_effect=forward):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=business_app), base_url='http://business') as client:
                    body = {'menu': 'RICE', 'meal_id': meals[0], 'meal_version': 12,
                            'prior_meal_id': meals[1], 'prior_meal_version': 12}
                    headers = {'X-CSRF-Token': 'csrf'}
                    created = await client.post('/api/customer/ai/combi/recipe-requests', json=body, headers=headers)
                    self.assertEqual(created.status_code, 201, created.text)
                    parent = created.json()['execution_id']
                    preview = await client.post('/api/customer/ai/combi/recipe-requests/' + parent + '/local-preview',
                                                json=body, headers=headers)
                    self.assertEqual(preview.status_code, 201, preview.text)
                    quote = preview.json()
                    result = await client.post('/api/customer/ai/combi/recipe-quotes/' + quote['quote_id'] + '/confirm',
                        json={**body, 'notice_version': quote['notice_version'], 'quality_warning_acknowledged': True},
                        headers=headers)
                    self.assertEqual(result.status_code, 201, result.text)
                    self.assertFalse(result.json()['execution_available'])
                    execution = self.registry.execution(result.json()['execution_id'])
                    self.assertEqual(execution.combi_binding.current_meal_id, meals[0])
                    self.assertEqual(execution.combi_binding.prior_meal_id, meals[1])
                    self.assertEqual(self.registry.consent(quote['quote_id']).execution_id, execution.execution_id)
                    # A new request cannot reuse changed source bytes with the old digest.
                    before = set(self.resource.table.items)
                    source_pk = f'SCOPE#USER#customer-a#COMBI_MEAL#{meals[0]}#SOURCE'
                    store.items[(source_pk, 'RAW#0000')]['records'][0] += ' '
                    changed = await client.post('/api/customer/ai/combi/recipe-requests', json=body, headers=headers)
                    self.assertEqual(changed.status_code, 409, changed.text)
                    self.assertEqual(set(self.resource.table.items), before)
            self.assertEqual(provider.mock_calls, [])
            self.assertEqual(self.registry.execution_state(execution.execution_id), 'PENDING')
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service


if __name__ == '__main__':
    unittest.main()
