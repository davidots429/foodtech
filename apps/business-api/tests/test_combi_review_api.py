"""Customer CSRF -> owned stored proposal -> immutable Combi human review."""
import copy
from dataclasses import replace
from uuid import uuid4
import httpx
import test_combi_result as fixtures
from foodtech_business_api.main import app


class CombiReviewApiTests(fixtures.CombiResultFixtures):
    async def review_get(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.get(f'/api/customer/ai/combi/recipe-executions/{self.execution_id}/review',
                params={'meal_id':self.meal_id,'menu':'RICE'})

    async def review_post(self,body,headers=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post(f'/api/customer/ai/combi/recipe-executions/{self.execution_id}/review',
                json=body,headers=headers if headers is not None else {'X-CSRF-Token':'csrf'})

    def body(self,context):
        return {'meal_id':self.meal_id,'menu':'RICE','expected_meal_version':3,'expected_review_version':context['review_version'],
            'request_id':str(uuid4()),'proposal_digest':context['proposal_digest'],'decision':'APPROVE',
            'reason':'합성 조건과 조리 결과를 검토했습니다.','acknowledged':True}

    async def test_context_review_retry_and_history_preserve_owned_batch(self):
        self.succeed();context=await self.review_get()
        self.assertEqual(context.status_code,200,context.text)
        self.assertTrue(context.json()['can_review']);self.assertEqual(context.json()['review_version'],0)
        original=copy.deepcopy(self.store.items[self.pk,'META']);body=self.body(context.json())
        saved=await self.review_post(body)
        self.assertEqual(saved.status_code,201,saved.text)
        self.assertFalse(saved.json()['adoption_available'])
        self.assertEqual(saved.headers['cache-control'],'no-store')
        self.assertEqual(saved.json()['review']['menu'],'RICE')
        self.assertEqual(saved.json()['review']['meal_id'],self.meal_id)
        self.assertEqual((await self.review_post(body)).json(),saved.json())
        self.assertEqual(self.store.items[self.pk,'META'],original)
        self.assertEqual(len(self.store.items),3)
        latest=(await self.review_get()).json()
        self.assertEqual(latest['review_version'],1);self.assertEqual(latest['latest_review'],saved.json()['review'])
        body={**self.body(latest),'decision':'REJECT','reason':'다음 조리 전에 다시 확인합니다.'}
        self.assertEqual((await self.review_post(body)).status_code,201)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            path='/api/customer/ai/combi/recipe-reviews'
            first=await client.get(path,params={'meal_id':self.meal_id,'menu':'RICE','limit':1})
            self.assertEqual(first.json()['items'][0]['version'],1);self.assertEqual(first.json()['next_cursor'],1)
            second=await client.get(path,params={'meal_id':self.meal_id,'menu':'RICE','limit':1,'cursor':1})
            self.assertEqual(second.json()['items'][0]['version'],2);self.assertIsNone(second.json()['next_cursor'])
            empty=await client.get(path,params={'meal_id':self.meal_id,'menu':'MACKEREL'})
            self.assertEqual(empty.json()['items'],[])
        self.registry.create_execution.assert_not_called()

    async def test_csrf_explicit_ack_reason_and_extra_identity_cannot_write(self):
        self.succeed();body=self.body((await self.review_get()).json())
        self.assertEqual((await self.review_post(body,{})).status_code,403)
        for change in ({'acknowledged':False},{'reason':' '},{'reason':' 앞 공백'},
                {'tenant_id':'foreign'},{'expected_review_version':True},{'job_id':self.job_id}):
            response=await self.review_post({**body,**change})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(len(self.store.items),1)

    async def test_changed_digest_version_or_wrong_meal_never_writes(self):
        self.succeed();body=self.body((await self.review_get()).json())
        for change in ({'proposal_digest':'0'*64},{'expected_meal_version':4},{'expected_review_version':1}):
            self.assertEqual((await self.review_post({**body,**change})).status_code,409)
        self.assertEqual((await self.review_post({**body,'meal_id':str(uuid4())})).status_code,404)
        self.store.items[self.pk,'META']['version']=4
        context=await self.review_get()
        self.assertFalse(context.json()['can_review'])
        self.assertEqual((await self.review_post(body)).status_code,409)
        self.assertEqual(len(self.store.items),1)

    async def test_nonterminal_and_foreign_execution_are_not_reviewable(self):
        self.assertEqual((await self.review_get()).status_code,409)
        self.succeed();self.execution=replace(self.execution,tenant_id='foreign')
        self.assertEqual((await self.review_get()).status_code,404)
        self.assertEqual(len(self.store.items),1)

    async def test_session_revocation_before_commit_hides_context_and_leaves_no_review(self):
        self.succeed();body=self.body((await self.review_get()).json())
        self.on_response=lambda:setattr(self,'epoch',1)
        self.assertEqual((await self.review_post(body)).status_code,401)
        self.assertEqual(len(self.store.items),1)

    async def test_lost_save_response_is_recoverable_from_history_without_duplicate(self):
        from unittest.mock import patch
        from botocore.exceptions import ClientError
        from foodtech_business_api.combi_review_store import CombiReviewStore
        self.succeed();body=self.body((await self.review_get()).json())
        save=CombiReviewStore.save
        def uncertain(store,*args):
            save(store,*args)
            raise ClientError({'Error':{'Code':'ServiceUnavailable','Message':'private-marker'}},'TransactWriteItems')
        with patch.object(CombiReviewStore,'save',uncertain):
            response=await self.review_post(body)
        self.assertEqual(response.status_code,503)
        self.assertNotIn('private-marker',response.text)
        context=(await self.review_get()).json()
        self.assertEqual(context['review_version'],1)
        self.assertEqual(context['latest_review']['request_id'],body['request_id'])
        retry=await self.review_post(body)
        self.assertEqual(retry.status_code,201,retry.text)
        self.assertEqual(retry.json()['review']['version'],1)
        self.assertEqual(len(self.store.items),3)
