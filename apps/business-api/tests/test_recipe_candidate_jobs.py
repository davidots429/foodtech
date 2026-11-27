import copy
from unittest.mock import patch
from uuid import uuid4
from test_recipe_status import RecipeStatusFixtures
from foodtech_business_api.recipe_review_store import RecipeReviewStore
from foodtech_business_api.recipe_candidate_jobs import CandidateJobStore
from foodtech_contracts.recipe_result_view import recipe_proposal_digest
import httpx
from foodtech_business_api.main import app


class CandidateJobTests(RecipeStatusFixtures):
    def setUp(self):
        super().setUp()
        self.store.items[self.pk,'META'].update(owner_subject=self.subject,job_id=self.job_id,version=3,
            menu='JEYUK',provenance='SYNTHETIC',equipment_profile='jeyuk-medium',recipe_version='baseline-v1',
            status='COMPLETED',safety_status='PASSED',safety_version=7,evaluation_blocked=False,evaluation_status='COMPLETED')
        self.proposal={'status':'NEEDS_REVIEW','schema_version':'synthetic-recommendation-output-v1',
            'guard_version':'synthetic-recommendation-guard-v1','menu':'JEYUK',
            'data':{'temperature_c':'200','rpm':'60','duration_seconds':300,'load_kg':'7',
                    'vegetable_interval_seconds':60,'sauce_interval_seconds':120}}
        self.review={'schema_version':'recipe-review-v1','version':1,'request_id':str(uuid4()),
            'execution_id':self.execution_id,'job_id':self.job_id,'job_version':3,
            'proposal_digest':recipe_proposal_digest(self.proposal),'proposal':self.proposal,
            'decision':'APPROVE','reason':'합성 조건 확인','reviewed_at':'2026-11-28T00:00:00Z'}
        RecipeReviewStore(self.store).save(self.pk,3,0,self.review,0)
        self.body={'request_id':str(uuid4()),'expected_job_version':3,'proposal_digest':self.review['proposal_digest'],
            'planned_portions':20,'actual_load_kg':'7.00','acknowledged':True}

    async def request(self,body=None,csrf='csrf',job_id=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post('/api/customer/ai/recipe-candidates/1/cooking-jobs',params={'job_id':job_id or self.job_id},
                json=self.body if body is None else body,headers={'X-CSRF-Token':csrf})

    async def test_selection_creates_fresh_listed_job_without_source_or_safety_mutation(self):
        before=copy.deepcopy(self.store.items)
        result=await self.request()
        self.assertEqual(result.status_code,201,result.text)
        self.assertTrue(result.json()['created'])
        self.assertEqual(result.headers['cache-control'],'no-store')
        target=f"SCOPE#USER#{self.subject}#JOB#{self.body['request_id']}"
        meta=self.store.items[target,'META']
        self.assertEqual(meta['status'],'PENDING');self.assertEqual(meta['safety_status'],'NOT_MEASURED')
        self.assertEqual(meta['safety_version'],0);self.assertTrue(meta['evaluation_blocked'])
        self.assertEqual(meta['evaluation_status'],'NOT_COMPLETED')
        self.assertIsNone(meta['source_job_id']);self.assertIsNone(meta['source_binding'])
        self.assertIsNone(meta['validation_context'])
        selection=meta['recipe_candidate_selection'];self.assertEqual(selection['source_job_id'],self.job_id)
        self.assertEqual(selection['review_version'],1);self.assertEqual(selection['candidate']['proposal'],self.proposal)
        self.assertEqual(meta['recipe_version'],selection['candidate']['candidate_id'])
        for key,value in before.items():self.assertEqual(self.store.items[key],value)
        page=self.store.list_jobs(self.subject,20,None);self.assertEqual(page['items'][0]['job_id'],self.body['request_id'])
        self.assertEqual(self.store.history_revision(self.subject),1)
        self.registry.create_execution.assert_not_called();self.assertEqual(self.requests,[])

    async def test_same_request_is_idempotent_and_changed_input_conflicts(self):
        self.assertEqual((await self.request()).status_code,201)
        before=copy.deepcopy(self.store.items)
        response=await self.request({**self.body,'actual_load_kg':'7'})
        self.assertEqual(response.status_code,201,response.text);self.assertFalse(response.json()['created'])
        self.assertEqual(self.store.items,before)
        self.assertEqual((await self.request({**self.body,'planned_portions':21})).status_code,409)
        self.assertEqual(self.store.items,before)

    async def test_csrf_ack_unknown_fields_geometry_and_foreign_job_fail_before_create(self):
        before=copy.deepcopy(self.store.items)
        self.assertEqual((await self.request(csrf='wrong')).status_code,403)
        for change in ({'acknowledged':False},{'acknowledged':1},{'tenant_id':'foreign'},
                       {'planned_portions':True},{'planned_portions':1.5},{'actual_load_kg':None},
                       {'thickness_cm':'1.2'},{'actual_load_kg':'999.99'}):
            with self.subTest(change=change):self.assertEqual((await self.request({**self.body,**change})).status_code,422)
        self.assertEqual((await self.request(job_id=str(uuid4()))).status_code,404)
        self.assertEqual(self.store.items,before)

    async def test_changed_source_job_or_later_rejection_cannot_create(self):
        before=copy.deepcopy(self.store.items)
        self.store.items[self.pk,'META']['version']=4
        self.assertEqual((await self.request()).status_code,409)
        self.store.items=copy.deepcopy(before)
        RecipeReviewStore(self.store).save(self.pk,3,1,{**self.review,'version':2,'request_id':str(uuid4()),'decision':'REJECT'},0)
        before=copy.deepcopy(self.store.items)
        self.assertEqual((await self.request()).status_code,409)
        self.assertEqual(self.store.items,before)

    async def test_source_change_during_atomic_create_is_rejected(self):
        original=CandidateJobStore.create
        def changed(adapter,*args):
            adapter.store.items[self.pk,'META']['version']=4
            return original(adapter,*args)
        with patch.object(CandidateJobStore,'create',changed):result=await self.request()
        self.assertEqual(result.status_code,409)
        self.assertNotIn((f"SCOPE#USER#{self.subject}#JOB#{self.body['request_id']}",'META'),self.store.items)

    async def test_revocation_after_commit_hides_receipt_and_preserves_recoverable_job(self):
        original=CandidateJobStore.create
        owner=self
        def changed(adapter,*args):
            value=original(adapter,*args);owner.epoch=1;return value
        with patch.object(CandidateJobStore,'create',changed):result=await self.request()
        self.assertEqual(result.status_code,401)
        self.assertNotIn('request_hash',result.text)
        self.assertIn((f"SCOPE#USER#{self.subject}#JOB#{self.body['request_id']}",'META'),self.store.items)


    def test_dynamo_creation_joins_eight_guards_writes_and_history_in_one_transaction(self):
        from unittest.mock import MagicMock
        from boto3.dynamodb.types import TypeSerializer
        import botocore.session
        from botocore.validate import validate_parameters
        from foodtech_business_api.cooking import NewJob,new_job_meta
        from foodtech_business_api.cooking_store import DynamoCookingStore
        from foodtech_business_api.recipe_candidate_jobs import CandidateJobRequest,request_hash
        store=object.__new__(DynamoCookingStore);store.table=MagicMock();store.table.name='cooking'
        store.client=MagicMock();store.auth_table_name='auth';store.require_principal=True;store.serializer=TypeSerializer()
        from botocore.exceptions import ClientError
        class Canceled(ClientError):pass
        store.client.exceptions.TransactionCanceledException=Canceled
        def get(**args):
            key=args['Key'];row=self.store.items.get((key['pk'],key['sk']))
            return {'Item':copy.deepcopy(row)} if row is not None else {}
        store.table.get_item.side_effect=get
        adapter=CandidateJobStore(store);candidate=adapter.reviews.candidate(self.pk,1)
        body=CandidateJobRequest(**self.body);fingerprint=request_hash(body,self.job_id,1)
        meta=new_job_meta(self.subject,NewJob(recipe_version=candidate['candidate_id'],equipment_profile='jeyuk-medium',planned_portions=20,actual_load_kg='7'),job_id=str(body.request_id))
        meta['recipe_candidate_selection']={'schema_version':'recipe-candidate-selection-v1','request_id':str(body.request_id),'request_hash':fingerprint,'source_job_id':self.job_id,'source_job_version':3,'review_version':1,'candidate':candidate}
        self.assertTrue(adapter.create(self.pk,1,candidate,meta,fingerprint,5))
        actions=store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(actions),8)
        self.assertEqual([next(iter(a)) for a in actions],['ConditionCheck']*5+['Put','Put','Update'])
        self.assertEqual(actions[0]['ConditionCheck']['ExpressionAttributeValues'][':expected'],{'N':'5'})
        self.assertTrue(all(next(iter(a.values()))['ReturnValuesOnConditionCheckFailure']=='NONE' for a in actions))
        validate_parameters({'TransactItems':actions},botocore.session.get_session().get_service_model('dynamodb').operation_model('TransactWriteItems').input_shape)
        # An auth/source condition failure is never converted to an idempotent success.
        for failed_index in range(5):
            reasons=[{'Code':'None'} for _ in range(8)];reasons[failed_index]={'Code':'ConditionalCheckFailed'}
            store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},'CancellationReasons':reasons},'TransactWriteItems')
            from foodtech_business_api.cooking_store import Conflict
            with self.subTest(failed_index=failed_index),self.assertRaises(Conflict):adapter.create(self.pk,1,candidate,meta,fingerprint,5)


    def test_concurrent_same_request_creates_one_job_listing_and_history_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        from foodtech_business_api.cooking import NewJob,new_job_meta
        from foodtech_business_api.recipe_candidate_jobs import CandidateJobRequest,request_hash
        body=CandidateJobRequest(**self.body);adapter=CandidateJobStore(self.store)
        candidate=adapter.reviews.candidate(self.pk,1);fingerprint=request_hash(body,self.job_id,1)
        meta=new_job_meta(self.subject,NewJob(recipe_version=candidate['candidate_id'],equipment_profile='jeyuk-medium',planned_portions=20,actual_load_kg='7'),job_id=str(body.request_id))
        meta['recipe_candidate_selection']={'schema_version':'recipe-candidate-selection-v1','request_id':str(body.request_id),'request_hash':fingerprint,'source_job_id':self.job_id,'source_job_version':3,'review_version':1,'candidate':candidate}
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda _:adapter.create(self.pk,1,candidate,copy.deepcopy(meta),fingerprint,0),range(2)))
        self.assertCountEqual(results,[True,False])
        self.assertEqual(self.store.history_revision(self.subject),1)
        self.assertEqual(len(self.store.list_jobs(self.subject,20,None)['items']),1)


    async def test_tonkatsu_candidate_uses_normal_new_batch_validation(self):
        for key in list(self.store.items):
            if key[0]==self.pk and key[1]!='META':del self.store.items[key]
        self.store.items[self.pk,'META'].update(menu='TONKATSU',equipment_profile='synthetic-fryer-approved-v2')
        proposal={**self.proposal,'menu':'TONKATSU','data':{'temperature_c':'170','duration_seconds':300}}
        review={**self.review,'proposal':proposal,'proposal_digest':recipe_proposal_digest(proposal)}
        RecipeReviewStore(self.store).save(self.pk,3,0,review,0)
        body={**self.body,'proposal_digest':review['proposal_digest'],'planned_portions':4,'actual_load_kg':None,'thickness_cm':'1.20'}
        response=await self.request(body)
        self.assertEqual(response.status_code,201,response.text)
        child=self.store.items[f"SCOPE#USER#{self.subject}#JOB#{body['request_id']}",'META']
        self.assertEqual(child['menu'],'TONKATSU');self.assertEqual(child['thickness_profile'],'STANDARD')
        self.assertEqual(child['safety_status'],'NOT_MEASURED');self.assertIsNone(child['actual_load_kg'])


    async def test_corrupt_existing_lineage_is_not_reported_as_idempotent_success(self):
        self.assertEqual((await self.request()).status_code,201)
        target=f"SCOPE#USER#{self.subject}#JOB#{self.body['request_id']}"
        self.store.items[target,'META']['recipe_candidate_selection']['source_job_version']=4
        before=copy.deepcopy(self.store.items)
        self.assertEqual((await self.request()).status_code,409)
        self.assertEqual(self.store.items,before)
