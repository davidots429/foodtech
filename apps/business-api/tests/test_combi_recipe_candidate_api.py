"""Owned approved review -> explicit candidate, no additional model request."""
import copy
from uuid import uuid4
import httpx
import test_combi_result as fixtures
from foodtech_business_api.main import app


class CombiCandidateApiTests(fixtures.CombiResultFixtures):
    def setUp(self):
        super().setUp();self.store.items[self.pk,'META']['equipment_size']='MEDIUM_FIXED_RACK_10'

    async def approve(self):
        self.succeed()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            path=f'/api/customer/ai/combi/recipe-executions/{self.execution_id}/review'
            context=(await client.get(path,params={'meal_id':self.meal_id,'menu':'RICE'})).json()
            body={'meal_id':self.meal_id,'menu':'RICE','expected_meal_version':3,'expected_review_version':0,
                'request_id':str(uuid4()),'proposal_digest':context['proposal_digest'],'decision':'APPROVE',
                'reason':'합성 권고 검토','acknowledged':True}
            response=await client.post(path,json=body,headers={'X-CSRF-Token':'csrf'})
            self.assertEqual(response.status_code,201,response.text)
            self.review_body=body
        return {'meal_id':self.meal_id,'menu':'RICE','expected_meal_version':3,
                'proposal_digest':context['proposal_digest'],'acknowledged':True}

    async def preserve(self,body,headers=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post('/api/customer/ai/combi/recipe-candidates/1',json=body,
                headers={'X-CSRF-Token':'csrf'} if headers is None else headers)

    async def candidate(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.get('/api/customer/ai/combi/recipe-candidates/1',params={'meal_id':self.meal_id,'menu':'RICE'})

    async def test_explicit_candidate_is_owned_unapplied_and_preserves_baseline_and_retry(self):
        body=await self.approve();before=copy.deepcopy(self.store.items);calls=len(self.requests)
        response=await self.preserve(body)
        self.assertEqual(response.status_code,201,response.text)
        self.assertTrue(response.json()['adoption_available'])
        candidate=response.json()['candidate']
        self.assertEqual(candidate['status'],'CANDIDATE_NOT_APPLIED');self.assertIsNone(candidate['base_recipe_version'])
        self.assertEqual(candidate['meal_id'],self.meal_id);self.assertEqual(candidate['menu'],'RICE')
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual({key:value for key,value in self.store.items.items() if key in before},before)
        self.assertEqual((await self.candidate()).json()['candidate'],candidate)
        self.assertEqual((await self.preserve(body)).json()['candidate'],candidate)
        self.assertEqual(len(self.store.items),len(before)+1);self.assertEqual(len(self.requests),calls)
        self.registry.create_execution.assert_not_called()

    async def test_csrf_ack_digest_version_owner_and_extra_fields_do_not_create_candidate(self):
        body=await self.approve();before=copy.deepcopy(self.store.items)
        self.assertEqual((await self.preserve(body,{})).status_code,403)
        for change in ({'acknowledged':False},{'tenant_id':'foreign'},{'proposal':{}},{'expected_meal_version':True}):
            self.assertEqual((await self.preserve({**body,**change})).status_code,422)
        for change in ({'proposal_digest':'0'*64},{'expected_meal_version':4}):
            self.assertEqual((await self.preserve({**body,**change})).status_code,409)
        self.assertEqual((await self.preserve({**body,'meal_id':str(uuid4())})).status_code,404)
        self.assertEqual(self.store.items,before)
        self.assertEqual((await self.candidate()).status_code,404)

    async def test_later_rejection_blocks_preservation_but_candidate_remains_historical(self):
        body=await self.approve();saved=(await self.preserve(body)).json()['candidate']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            reject=await client.post(f'/api/customer/ai/combi/recipe-executions/{self.execution_id}/review',
                json={**self.review_body,'expected_review_version':1,'request_id':str(uuid4()),'decision':'REJECT'},
                headers={'X-CSRF-Token':'csrf'})
            self.assertEqual(reject.status_code,201,reject.text)
        self.assertEqual((await self.preserve(body)).status_code,409)
        historical=await self.candidate()
        self.assertEqual(historical.json()['candidate'],saved);self.assertEqual(historical.json()['current_review_version'],2)
        self.store.items[self.pk,'META']['version']=4
        self.assertEqual((await self.preserve(body)).status_code,409)
        self.assertEqual((await self.candidate()).json()['current_meal_version'],4)

    async def test_lost_candidate_response_recovers_same_record_without_second_model_or_write(self):
        from unittest.mock import patch
        from botocore.exceptions import ClientError
        from foodtech_business_api.combi_recipe_candidate import CombiCandidateStore
        body=await self.approve();calls=len(self.requests);save=CombiCandidateStore.preserve
        def uncertain(adapter,*args):
            save(adapter,*args)
            raise ClientError({'Error':{'Code':'ServiceUnavailable','Message':'private-marker'}},'TransactWriteItems')
        with patch.object(CombiCandidateStore,'preserve',uncertain):response=await self.preserve(body)
        self.assertEqual(response.status_code,503);self.assertNotIn('private-marker',response.text)
        saved=(await self.candidate()).json()['candidate']
        self.assertEqual((await self.preserve(body)).json()['candidate'],saved)
        self.assertEqual(len(self.requests),calls);self.assertEqual(len(self.store.items),4)
