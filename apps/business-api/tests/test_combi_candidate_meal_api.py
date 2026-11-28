"""Owned candidate adoption, idempotent recovery and authorization across I/O."""
import copy
import os
from uuid import uuid4
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from botocore.exceptions import ClientError
from test_combi_candidate_meal_plan import plan_input
from foodtech_business_api import cooking, combi_ai
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.combi_candidate_meal_store import CombiCandidateMealStore
from foodtech_business_api.combi_recipe_candidate import CombiCandidateStore
from foodtech_business_api.combi_review_store import CombiReviewStore
from foodtech_business_api.recipe_scope import RecipeScopeDenied
from foodtech_business_api.main import app


class CandidateMealApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=MemoryCookingStore();self.subject='candidate-customer';self.epoch=0
        async def session(*args):return Session('customer',self.subject,None,'csrf',9999999999,self.epoch)
        self.principal=AsyncMock()
        self.patches=[patch.dict(os.environ,{'FOODTECH_SECURE_COOKIES':'false'}),
            patch.object(cooking,'_session',side_effect=session),patch.object(combi_ai,'_session',side_effect=session),
            patch.object(cooking,'get_cooking_store',return_value=self.store),
            patch.object(combi_ai,'_require_principal_record',self.principal),
            patch.object(cooking,'_now',return_value='2026-11-28T03:00:00Z')]
        for item in self.patches:item.start()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')
        self.path='/api/customer/ai/combi/recipe-candidates/1/meals'
        self.headers={'X-CSRF-Token':'csrf'}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):item.stop()

    def source(self,menu):
        body,candidate,review=plan_input(menu)
        self.pk=f"SCOPE#USER#candidate-customer#COMBI_MEAL#{body['meal_id']}"
        self.store.items[self.pk,'META']={'pk':self.pk,'sk':'META','meal_batch_id':body['meal_id'],'version':3,
            'provenance':'SYNTHETIC','equipment_size':'MEDIUM_FIXED_RACK_10',
            ('rice_cooking_job_id' if menu=='RICE' else 'mackerel_cooking_job_id'):candidate['job_id']}
        CombiReviewStore(self.store).save(self.pk,menu,3,0,review,0)
        CombiCandidateStore(self.store).preserve(self.pk,menu,1,3,review['proposal_digest'],candidate['recorded_at'],0)
        return body

    async def test_both_menus_create_fresh_owned_records_and_recover_after_target_and_source_advance(self):
        for menu in ('RICE','MACKEREL'):
            body=self.source(menu);before=copy.deepcopy(self.store.items)
            response=await self.client.post(self.path,headers=self.headers,json=body)
            self.assertEqual(response.status_code,201,response.text)
            value=response.json();self.assertTrue(value['created']);self.assertEqual(value['meal_id'],body['request_id'])
            self.assertEqual(value['meal']['meal']['version'],1)
            self.assertEqual(value['meal']['rice']['safety_status'],'NOT_MEASURED')
            self.assertEqual(value['meal']['mackerel']['safety_status'],'NOT_MEASURED')
            self.assertEqual(response.headers['cache-control'],'no-store')
            for key,row in before.items():
                if key[1] != 'HISTORY#REVISION':self.assertEqual(self.store.items[key],row)
            target=f"SCOPE#USER#candidate-customer#COMBI_MEAL#{body['request_id']}"
            self.store.items[target,'META'].update(version=8,status='RICE_COOKING')
            self.store.items[self.pk,'META']['version']=9
            before_retry=copy.deepcopy(self.store.items)
            retry=await self.client.post(self.path,headers=self.headers,json=body)
            self.assertEqual(retry.status_code,200,retry.text);self.assertFalse(retry.json()['created'])
            self.assertEqual(retry.json()['meal']['meal']['version'],8)
            self.assertEqual(retry.json()['request_hash'],value['request_hash'])
            self.assertEqual(before_retry,self.store.items)

    async def test_csrf_owner_source_digest_ack_and_request_id_are_not_bypassed(self):
        body=self.source('RICE');before=copy.deepcopy(self.store.items)
        self.assertEqual((await self.client.post(self.path,json=body)).status_code,403)
        for change in ({'acknowledged':False},{'tenant_id':'other'},{'request_id':body['meal_id']}):
            response=await self.client.post(self.path,headers=self.headers,json={**body,**change})
            self.assertEqual(response.status_code,422,response.text)
        for change in ({'expected_meal_version':4},{'proposal_digest':'0'*64}):
            self.assertEqual((await self.client.post(self.path,headers=self.headers,json={**body,**change})).status_code,409)
        self.subject='other-customer'
        response=await self.client.post(self.path,headers=self.headers,json=body)
        self.assertNotIn(response.status_code,(200,201))
        self.assertEqual(self.store.items,before)

    async def test_lost_response_recovers_same_request_and_rejects_changed_fresh_measurements(self):
        body=self.source('MACKEREL');save=CombiCandidateMealStore.create
        def uncertain(adapter,*args):
            save(adapter,*args)
            raise ClientError({'Error':{'Code':'ServiceUnavailable','Message':'private-marker'}},'TransactWriteItems')
        with patch.object(CombiCandidateMealStore,'create',uncertain):
            response=await self.client.post(self.path,headers=self.headers,json=body)
        self.assertEqual(response.status_code,503);self.assertNotIn('private-marker',response.text)
        before=copy.deepcopy(self.store.items)
        retry=await self.client.post(self.path,headers=self.headers,json=body)
        self.assertEqual(retry.status_code,200,retry.text);self.assertEqual(before,self.store.items)
        changed=copy.deepcopy(body);changed['new_meal']['dry_rice_kg']='3'
        self.assertEqual((await self.client.post(self.path,headers=self.headers,json=changed)).status_code,409)
        self.assertEqual(before,self.store.items)

    async def test_revocation_or_session_change_after_create_returns_no_receipt_and_denies_recovery(self):
        body=self.source('RICE');self.principal.side_effect=[None,RecipeScopeDenied('revoked')]
        response=await self.client.post(self.path,headers=self.headers,json=body)
        self.assertEqual(response.status_code,401);self.assertNotIn('request_hash',response.text)
        before=copy.deepcopy(self.store.items)
        self.principal.side_effect=RecipeScopeDenied('revoked')
        self.assertEqual((await self.client.post(self.path,headers=self.headers,json=body)).status_code,401)
        self.assertEqual(before,self.store.items)
