"""BFF-to-Gateway recovery reads; no provider, STS issuance or source payload read."""
import json
import os
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4
import httpx

from test_recipe_status import RecipeStatusFixtures
from foodtech_business_api.main import app
from foodtech_ai_gateway.policy import Feature
from foodtech_contracts.recommendation_output import SCHEMA_VERSION
from foodtech_contracts.recommendation_guard import RULE_VERSION


class RecipeResultTests(RecipeStatusFixtures):
    def setUp(self):
        super().setUp()
        self.store.items[(self.pk,'META')]['menu']='JEYUK'
        self.proposal={'status':'NEEDS_REVIEW','error_code':None,'schema_version':SCHEMA_VERSION,
            'guard_version':RULE_VERSION,'fallback_used':False,'attempts':1,
            'data':{'temperature_c':'200','rpm':'60','duration_seconds':300,'load_kg':'7',
                    'vegetable_interval_seconds':60,'sauce_interval_seconds':120}}

    async def get(self,**params):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.get(f'/api/customer/ai/recipe-executions/{self.execution_id}/result',
                                    params={'job_id':self.job_id,**params})

    def succeed(self):
        self.state='SUCCEEDED'
        self.result={'state':self.state,'response_text':json.dumps(self.proposal),'credential':'private-key-marker'}

    async def test_saved_proposal_is_projected_without_new_model_execution(self):
        self.succeed()
        response=await self.get(tenant_id='foreign')
        self.assertEqual(response.status_code,200)
        payload=response.json()
        self.assertEqual(payload['projection_status'],'AVAILABLE')
        self.assertEqual(payload['proposal']['data'],self.proposal['data'])
        self.assertIs(payload['execution_available'],False)
        self.assertIs(payload['manual_invocation_enabled'],False)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertNotIn('private-key-marker',response.text)
        self.assertNotIn('response_text',response.text)
        self.registry.create_execution.assert_not_called()
        self.assertEqual(json.loads(self.requests[0].content)['tenant_id'],'customer_a')

    async def test_corrupt_terminal_output_does_not_expose_upstream_message(self):
        self.succeed();self.result['response_text']='private-provider-marker'
        response=await self.get()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['projection_status'],'UNAVAILABLE')
        self.assertIsNone(response.json()['proposal'])
        self.assertNotIn('private-',response.text)

    async def test_failed_or_unfinished_results_have_no_proposal(self):
        response=await self.get()
        self.assertEqual(response.json()['projection_status'],'NOT_READY')
        self.state='FAILED';self.result={'state':'FAILED','error_code':'private-marker','response_text':'private-marker'}
        response=await self.get()
        self.assertEqual(response.json()['projection_status'],'FAILED')
        self.assertNotIn('private-marker',response.text)

    async def test_other_owner_job_or_feature_is_rejected_before_result_read(self):
        original=self.execution
        for changed in [replace(original,tenant_id='foreign'),replace(original,job_id=str(uuid4())),
                        replace(original,feature=Feature.GENERAL_GUIDE)]:
            self.execution=changed
            self.assertEqual((await self.get()).status_code,404)
        self.registry.execution_status_snapshot.assert_not_called()

    async def test_session_revoke_hides_record_and_job_change_is_historical(self):
        self.succeed()
        self.on_response=lambda:self.store.items[(self.pk,'META')].update(version=4)
        response=await self.get()
        self.assertTrue(response.json()['job_changed'])
        self.assertEqual(response.json()['proposal']['status'],'NEEDS_REVIEW')
        self.on_response=lambda:setattr(self,'epoch',1)
        self.assertEqual((await self.get()).status_code,401)

    async def test_menu_mismatch_never_displays_a_candidate(self):
        self.succeed();self.store.items[(self.pk,'META')]['menu']='TONKATSU'
        response=await self.get()
        self.assertEqual(response.status_code,502)
        self.assertNotIn('temperature_c',response.text)

    async def test_manual_mode_metadata_never_grants_execution_from_status(self):
        env={name:'true' for name in ['FOODTECH_RECIPE_INVOCATION_ENABLED','FOODTECH_PRINCIPAL_PROVISIONING_ENABLED',
                                      'FOODTECH_RECIPE_SOURCE_BROKER_ENABLED']}
        with patch.dict(os.environ,env):
            response=await self.get()
        self.assertTrue(response.json()['manual_invocation_enabled'])
        self.assertFalse(response.json()['execution_available'])
        self.registry.create_execution.assert_not_called()
        env['FOODTECH_RECIPE_SOURCE_BROKER_ENABLED']='false'
        with patch.dict(os.environ,env):
            self.assertFalse((await self.get()).json()['manual_invocation_enabled'])
