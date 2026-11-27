"""Owned meal -> BFF -> Gateway stored result; all transports/storage synthetic."""
import copy
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from test_recipe_status import RecipeStatusFixtures
from foodtech_business_api import combi_ai
from foodtech_business_api.main import app
from foodtech_contracts.combi_execution_binding import CombiExecutionBinding
from foodtech_contracts.combi_recommendation import SCHEMA_VERSION, RULE_VERSION


class CombiResultTests(RecipeStatusFixtures):
    def setUp(self):
        super().setUp()
        self.store.items.clear()  # Combi has no standalone JOB partition.
        self.meal_id, self.prior_id, self.prior_job = (str(uuid4()) for _ in range(3))
        self.pk = f'SCOPE#USER#{self.subject}#COMBI_MEAL#{self.meal_id}'
        self.store.items[(self.pk, 'META')] = {'pk': self.pk, 'sk': 'META', 'meal_batch_id': self.meal_id,
            'version': 3, 'provenance': 'SYNTHETIC', 'rice_cooking_job_id': self.job_id,
            'mackerel_cooking_job_id': str(uuid4())}
        self.binding = CombiExecutionBinding('RICE', self.meal_id, 3, self.job_id, 'a'*64,
                                            self.prior_id, 2, self.prior_job, 'b'*64, 0)
        self.execution = replace(self.execution, prior_job_id=self.prior_job, prior_job_version=2,
                                 combi_binding=self.binding)
        self.proposal = {'status': 'NEEDS_REVIEW', 'error_code': None, 'schema_version': SCHEMA_VERSION,
            'guard_version': RULE_VERSION, 'fallback_used': False, 'attempts': 1,
            'data': {'temperature_c':'100', 'duration_seconds':1500, 'climate_setpoint_percent':'100',
                'fan_level':4, 'pan_positions':[{'rack':1,'shelf':1}], 'effective_water_ratio':'1.20',
                'soak_seconds':1800, 'drain_seconds':300}}
        async def session(*_):
            return SimpleNamespace(subject=self.subject, epoch=self.epoch, csrf='csrf',
                                   surface='customer', expires=int(time.time())+300)
        self.patches += [patch.object(combi_ai, '_session', side_effect=session),
            patch('foodtech_business_api.cooking._session', side_effect=session),
            patch.object(combi_ai, '_require_principal_record', AsyncMock())]
        # Super already started earlier patches.
        for p in self.patches[-3:]: p.start()

    def succeed(self):
        self.state = 'SUCCEEDED'
        self.result = {'state':self.state, 'response_text':json.dumps(self.proposal),
                       'credential':'private-marker', 'provider_response':'private-marker'}

    async def get(self, *, result=True, **params):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            resource = 'result' if result else 'status'
            return await client.get(f'/api/customer/ai/combi/recipe-executions/{self.execution_id}/{resource}',
                params={'meal_id': self.meal_id, 'menu':'RICE', **params})

    async def test_owned_rice_result_without_fake_job_or_raw_output(self):
        self.succeed()
        response = await self.get(tenant_id='forged')
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value['proposal']['menu'], 'RICE')
        self.assertEqual(value['proposal']['data'], self.proposal['data'])
        self.assertFalse(value['execution_available'])
        self.assertFalse(value['manual_invocation_enabled'])
        self.assertEqual(value['meal_id'], self.meal_id)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('private-', response.text)
        self.assertNotIn('combi_binding', response.text)
        self.assertEqual(json.loads(self.requests[0].content)['tenant_id'], 'customer_a')
        self.registry.create_execution.assert_not_called()

    async def test_mackerel_and_status_use_same_bound_meal_not_other_menu(self):
        self.job_id = self.store.items[(self.pk,'META')]['mackerel_cooking_job_id']
        binding = replace(self.binding, menu='MACKEREL', current_job_id=self.job_id)
        self.execution = replace(self.execution, job_id=self.job_id, combi_binding=binding)
        self.proposal['data'] = {'temperature_c':'220', 'duration_seconds':700,
            'climate_setpoint_percent':'10', 'fan_level':3, 'pan_positions':[{'rack':1,'shelf':1}],
            'transition_duration_seconds':180}
        self.succeed()
        response = await self.get(menu='MACKEREL')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['proposal']['menu'],'MACKEREL')
        self.assertEqual((await self.get(result=False,menu='MACKEREL')).json()['state'],'SUCCEEDED')
        self.assertEqual((await self.get()).status_code,404)

    async def test_other_owner_meal_menu_or_legacy_binding_is_rejected_before_result(self):
        original = self.execution
        for execution in (replace(original,tenant_id='foreign'),
                replace(original,combi_binding=None),
                replace(original,combi_binding=replace(self.binding,current_meal_id=str(uuid4())))):
            self.execution = execution
            self.assertEqual((await self.get()).status_code,404)
        self.registry.execution_status_snapshot.assert_not_called()
        self.execution = original
        self.assertEqual((await self.get(meal_id=str(uuid4()))).status_code,404)
        self.assertEqual((await self.get(menu='JEYUK')).status_code,422)

    async def test_pending_failure_and_corrupt_output_never_disclose_provider_values(self):
        self.assertEqual((await self.get()).json()['projection_status'],'NOT_READY')
        self.state='FAILED';self.result={'state':'FAILED','response_text':'private-marker'}
        self.assertEqual((await self.get()).json()['projection_status'],'FAILED')
        self.succeed();self.result['response_text']='private-marker'
        response=await self.get()
        self.assertEqual(response.json()['projection_status'],'UNAVAILABLE')
        self.assertNotIn('private-',response.text)

    async def test_binding_version_corruption_and_mixed_schema_are_hidden(self):
        self.succeed()
        # Simulate a corrupt persisted object that bypassed dataclass construction.
        object.__setattr__(self.execution,'job_version',4)
        self.assertEqual((await self.get()).status_code,409)
        object.__setattr__(self.execution,'job_version',3)
        for key,value in [('schema_version','synthetic-recommendation-output-v1'),
                          ('guard_version','synthetic-recommendation-guard-v1')]:
            saved=copy.deepcopy(self.proposal);self.proposal[key]=value;self.succeed()
            self.assertEqual((await self.get()).json()['projection_status'],'UNAVAILABLE')
            self.proposal=saved

    async def test_session_revocation_and_concurrent_meta_change_hide_result(self):
        self.succeed()
        self.on_response=lambda:setattr(self,'epoch',1)
        self.assertEqual((await self.get()).status_code,401)
        self.epoch=0
        self.on_response=lambda:self.store.items[(self.pk,'META')].update(version=4)
        self.assertEqual((await self.get()).status_code,409)
        self.on_response=None
        response=await self.get()
        self.assertTrue(response.json()['job_changed'])
        self.assertEqual(response.json()['proposal']['status'],'NEEDS_REVIEW')

    async def test_plain_http_gateway_rejects_and_read_has_no_side_effect(self):
        from foodtech_ai_gateway.main import app as gateway
        gateway.state.mtls_listener=False
        self.assertEqual((await self.get()).status_code,502)
        self.assertEqual(len(self.store.items),1)
        self.registry.create_execution.assert_not_called()

    async def test_invalid_pan_numeric_and_unknown_fields_never_become_displayed_proposal(self):
        original=copy.deepcopy(self.proposal)
        for name, value in [('fan_level',True),('duration_seconds',1.5),('effective_water_ratio','NaN'),
                ('temperature_c','100E9999'),('pan_positions',[]),
                ('pan_positions',[{'rack':1,'shelf':0}]),('pan_positions',[{'rack':True,'shelf':1}]),
                ('pan_positions',[{'rack':1,'shelf':1},{'rack':1,'shelf':1}]),
                ('pan_positions',[{'rack':1,'shelf':1,'secret':'private-marker'}]),
                ('credential','private-marker')]:
            self.proposal=copy.deepcopy(original);self.proposal['data'][name]=value;self.succeed()
            response=await self.get()
            self.assertEqual(response.json()['projection_status'],'UNAVAILABLE',response.text)
            self.assertNotIn('private-',response.text)

    async def test_legacy_gateway_status_cannot_skip_meal_binding(self):
        from foodtech_ai_gateway.main import app as gateway
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway),base_url='https://gateway') as client:
            response=await client.post(f'/internal/ai/recipe-results/{self.execution_id}',
                json={'tenant_id':self.subject,'job_id':self.job_id})
        self.assertEqual(response.status_code,404)
        self.registry.execution_status_snapshot.assert_not_called()

    async def test_review_digest_copies_pan_layout_and_binds_exact_menu_versions(self):
        from foodtech_contracts.recipe_result_view import recipe_proposal_digest, validated_recipe_proposal
        self.succeed()
        proposal=(await self.get()).json()['proposal']
        digest=recipe_proposal_digest(proposal)
        copied=validated_recipe_proposal(proposal)
        copied['data']['pan_positions'][0]['shelf']=2
        self.assertEqual(proposal['data']['pan_positions'][0]['shelf'],1)
        self.assertNotEqual(recipe_proposal_digest(copied),digest)
        for changes in ({'menu':'MACKEREL'},{'schema_version':'future'},{'status':'APPROVED'}):
            with self.assertRaises(ValueError):recipe_proposal_digest({**proposal,**changes})
