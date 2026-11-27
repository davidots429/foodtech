"""Unapplied programme preservation with owned review/meal atomic conditions."""
import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import MagicMock
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
import botocore.session
from test_combi_review_store import record, MEAL, JOB, PK
from foodtech_business_api.combi_review_store import CombiReviewStore, _keys
from foodtech_business_api.combi_recipe_candidate import CombiCandidateStore, candidate_key, build_combi_candidate, validate_combi_candidate
from foodtech_business_api.cooking_store import MemoryCookingStore, DynamoCookingStore, Conflict

NOW='2026-11-28T02:00:00Z'


class CombiRecipeCandidateTests(unittest.TestCase):
    def setUp(self):
        self.store=MemoryCookingStore()
        self.meta={'pk':PK,'sk':'META','meal_batch_id':MEAL,'version':3,'provenance':'SYNTHETIC',
            'rice_cooking_job_id':JOB,'equipment_size':'MEDIUM_FIXED_RACK_10'}
        self.store.items[PK,'META']=copy.deepcopy(self.meta)
        self.value=record();self.reviews=CombiReviewStore(self.store)
        self.reviews.save(PK,'RICE',3,0,self.value,0)
        self.candidates=CombiCandidateStore(self.store)

    def preserve(self,adapter=None,**changes):
        args=dict(pk=PK,menu='RICE',version=1,expected_meal_version=3,digest=self.value['proposal_digest'],recorded_at=NOW,expected_epoch=0)
        return (adapter or self.candidates).preserve(**{**args,**changes})

    def test_exact_immutable_candidate_and_retry_leave_cooking_and_review_unchanged(self):
        before=copy.deepcopy(self.store.items)
        saved=self.preserve()
        self.assertEqual(saved['status'],'CANDIDATE_NOT_APPLIED')
        self.assertIsNone(saved['base_recipe_version'])
        self.assertEqual(saved['meal_id'],MEAL);self.assertEqual(saved['review_request_id'],self.value['request_id'])
        self.assertLessEqual(len(saved['candidate_id']),80)
        self.assertEqual({k:v for k,v in self.store.items.items() if k in before},before)
        self.assertEqual(len(self.store.items),len(before)+1)
        saved['proposal']['data']['pan_positions'][0]['shelf']=2
        reread=self.candidates.get(PK,'RICE',1)
        self.assertEqual(reread['proposal']['data']['pan_positions'][0]['shelf'],1)
        self.assertEqual(self.preserve(recorded_at='2026-11-28T03:00:00Z'),reread)

    def test_later_reject_or_meal_change_blocks_creation_without_restoring_old_approval(self):
        old=self.preserve()
        self.reviews.save(PK,'RICE',3,1,{**record(2),'decision':'REJECT'},0)
        with self.assertRaises(Conflict):self.preserve()
        self.assertEqual(self.candidates.get(PK,'RICE',1),old)
        self.store.items[PK,'META']['version']=4
        with self.assertRaises(Conflict):self.preserve()
        self.assertEqual(self.candidates.get(PK,'RICE',1)['status'],'CANDIDATE_NOT_APPLIED')

    def test_scope_digest_time_lineage_and_unearned_candidate_state_are_rejected(self):
        for change in ({'digest':'0'*64},{'expected_meal_version':4},{'pk':PK.replace('#customer#','#foreign#')},
                {'menu':'MACKEREL'},{'recorded_at':'2026-11-28T00:00:00Z'},{'recorded_at':'2026-11-28T03:00:00'}):
            before=copy.deepcopy(self.store.items)
            with self.assertRaises((Conflict,ValueError)):self.preserve(**change)
            self.assertEqual(self.store.items,before)
        self.store.items[PK,'META']['rice_recipe_version']='baseline-v1'
        value=self.preserve();self.assertEqual(value['base_recipe_version'],'baseline-v1')
        for change in ({'status':'APPLIED'},{'provenance':'REAL'},{'proposal_digest':'0'*64},{'credential':'private-marker'},
            {'candidate_id':'foreign'},{'menu':'MACKEREL'},{'review_request_id':MEAL}):
            with self.assertRaises(ValueError):validate_combi_candidate({**value,**change},self.value)
        with self.assertRaises(ValueError):build_combi_candidate({**self.value,'decision':'REJECT'},self.meta,NOW)

    def test_concurrent_preservation_has_one_record_and_same_candidate(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            values=list(pool.map(lambda at:self.preserve(recorded_at=at),[NOW,'2026-11-28T03:00:00Z']))
        self.assertEqual(values[0],values[1]);self.assertEqual(len(self.store.items),4)

    def dynamo(self):
        store=object.__new__(DynamoCookingStore);store.table=MagicMock();store.table.name='cooking'
        store.client=MagicMock();store.serializer=TypeSerializer();store.auth_table_name='auth';store.require_principal=True
        class Canceled(ClientError):pass
        store.client.exceptions.TransactionCanceledException=Canceled
        rows=copy.deepcopy(self.store.items)
        store.table.get_item.side_effect=lambda **kw:{'Item':copy.deepcopy(rows.get((kw['Key']['pk'],kw['Key']['sk'])))}
        return store,Canceled,rows

    def test_dynamo_atomic_auth_meta_pointer_full_review_and_candidate_sdk_contract(self):
        store,_,_=self.dynamo();self.preserve(CombiCandidateStore(store))
        actions=store.client.transact_write_items.call_args.kwargs['TransactItems']
        model=botocore.session.get_session().get_service_model('dynamodb').operation_model('TransactWriteItems')
        validate_parameters({'TransactItems':actions},model.input_shape)
        self.assertEqual(len(actions),5)
        auth,meta,pointer,review,saved=[next(iter(a.values())) for a in actions]
        self.assertEqual(auth['Key'],{'pk':{'S':'USER#customer'}})
        self.assertIn('deletion_lock_request',auth['ConditionExpression'])
        self.assertIn('attribute_not_exists(#absent0)',meta['ConditionExpression'])
        self.assertIn('rice_recipe_version',meta['ExpressionAttributeNames'].values())
        self.assertEqual(pointer['Key']['sk'],{'S':_keys('RICE')[0]})
        self.assertEqual(review['Key']['sk'],{'S':_keys('RICE')[1]+f'{1:019d}'})
        self.assertEqual(set(review['ExpressionAttributeNames'].values()),set(self.value))
        self.assertEqual(saved['Item']['sk'],{'S':candidate_key('RICE',1)})
        self.assertEqual(saved['Item']['status'],{'S':'CANDIDATE_NOT_APPLIED'})
        self.assertTrue(all(next(iter(a.values()))['ReturnValuesOnConditionCheckFailure']=='NONE' for a in actions))
        self.assertEqual([list(a)[0] for a in actions],['ConditionCheck']*4+['Put'])

    def test_dynamo_source_failure_never_recovers_existing_candidate_as_success(self):
        for failed in range(4):
            store,Canceled,rows=self.dynamo()
            rows[PK,candidate_key('RICE',1)]={'pk':PK,'sk':candidate_key('RICE',1),**build_combi_candidate(self.value,self.meta,NOW)}
            reasons=[{'Code':'None'} for _ in range(5)];reasons[failed]={'Code':'ConditionalCheckFailed'}
            reasons[4]={'Code':'ConditionalCheckFailed'}
            store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},'CancellationReasons':reasons},'TransactWriteItems')
            with self.assertRaises(Conflict):self.preserve(CombiCandidateStore(store))
            self.assertFalse(any(call.kwargs['Key']['sk'].startswith('RECIPE_CANDIDATE') for call in store.table.get_item.call_args_list))

    def test_dynamo_duplicate_only_returns_exact_retained_record_and_capacity_errors_propagate(self):
        store,Canceled,rows=self.dynamo();candidate=build_combi_candidate(self.value,self.meta,NOW)
        rows[PK,candidate_key('RICE',1)]={'pk':PK,'sk':candidate_key('RICE',1),**candidate}
        store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'None'}]*4+[{'Code':'ConditionalCheckFailed'}]},'TransactWriteItems')
        self.assertEqual(self.preserve(CombiCandidateStore(store),recorded_at='2026-11-28T03:00:00Z'),candidate)
        rows[PK,candidate_key('RICE',1)]['equipment_size']='LARGE_ROLL_IN_RACK_20'
        with self.assertRaises(Conflict):self.preserve(CombiCandidateStore(store))
        store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},'CancellationReasons':[{'Code':'ProvisionedThroughputExceeded'}]},'TransactWriteItems')
        with self.assertRaises(Canceled):self.preserve(CombiCandidateStore(store))
