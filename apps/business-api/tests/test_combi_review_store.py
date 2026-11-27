"""Combi review isolation, atomic SDK transaction shape and conflict recovery."""
import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import MagicMock
from uuid import uuid4
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
import botocore.session
from foodtech_business_api.combi_review_store import CombiReviewStore, _keys, validate_combi_review
from foodtech_business_api.cooking_store import MemoryCookingStore, DynamoCookingStore, Conflict
from foodtech_contracts.recipe_result_view import recipe_proposal_digest

MEAL, JOB, EXECUTION = [str(uuid4()) for _ in range(3)]
PK = f'SCOPE#USER#customer#COMBI_MEAL#{MEAL}'
PROPOSAL = {'status':'NEEDS_REVIEW','schema_version':'synthetic-combi-recommendation-output-v1',
    'guard_version':'synthetic-combi-recommendation-guard-v1','menu':'RICE','data':{'temperature_c':'100',
    'duration_seconds':1500,'climate_setpoint_percent':'100','fan_level':4,'pan_positions':[{'rack':1,'shelf':1}],
    'effective_water_ratio':'1.20','soak_seconds':1800,'drain_seconds':300}}


def record(version=1):
    return {'schema_version':'combi-recipe-review-v1','version':version,'request_id':str(uuid4()),
        'execution_id':EXECUTION,'meal_id':MEAL,'menu':'RICE','job_id':JOB,'job_version':3,
        'proposal_digest':recipe_proposal_digest(PROPOSAL),'proposal':copy.deepcopy(PROPOSAL),
        'decision':'APPROVE','reason':'합성 조건을 검토했습니다.','reviewed_at':'2026-11-28T01:00:00Z'}


class CombiReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.store=MemoryCookingStore()
        self.meta={'pk':PK,'sk':'META','meal_batch_id':MEAL,'version':3,'provenance':'SYNTHETIC',
                   'rice_cooking_job_id':JOB,'mackerel_cooking_job_id':str(uuid4())}
        self.store.items[PK,'META']=copy.deepcopy(self.meta)
        self.reviews=CombiReviewStore(self.store)

    def test_save_retry_copy_and_menu_isolation_without_cooking_or_candidate_writes(self):
        value=record();saved=self.reviews.save(PK,'RICE',3,0,value,0)
        saved['proposal']['data']['pan_positions'][0]['shelf']=2
        self.assertEqual(self.reviews.current(PK,'RICE')['review'],value)
        self.assertEqual(self.reviews.current(PK,'MACKEREL'),{'version':0,'review':None})
        self.assertEqual(self.reviews.save(PK,'RICE',3,0,{**value,'reviewed_at':'2026-11-28T02:00:00Z'},0),value)
        self.assertEqual(self.store.items[PK,'META'],self.meta)
        self.assertEqual(len(self.store.items),3)
        with self.assertRaises(Conflict):self.reviews.save(PK,'RICE',3,0,{**value,'reason':'다른 사유'},0)

    def test_concurrent_review_one_winner_and_bounded_history(self):
        def submit(value):
            try:self.reviews.save(PK,'RICE',3,0,value,0);return True
            except Conflict:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(submit,[record(),record()])),[True,False])
        self.reviews.save(PK,'RICE',3,1,{**record(2),'decision':'REJECT'},0)
        page=self.reviews.history(PK,'RICE',1)
        self.assertEqual([v['version'] for v in page['items']],[1]);self.assertEqual(page['next_cursor'],1)
        self.assertEqual(self.reviews.history(PK,'RICE',1,1)['items'][0]['version'],2)
        self.assertIsNone(self.reviews.history(PK,'RICE',1,1)['next_cursor'])

    def test_invalid_owner_meal_menu_job_version_digest_or_extra_fields_are_rejected(self):
        original=record()
        for change in ({'meal_id':str(uuid4())},{'menu':'MACKEREL'},{'job_id':str(uuid4())},
            {'job_version':4},{'proposal_digest':'0'*64},{'secret':'private-marker'},{'version':True}):
            before=copy.deepcopy(self.store.items)
            with self.assertRaises((ValueError,Conflict)):
                self.reviews.save(PK,'RICE',3,0,{**original,**change},0)
            self.assertEqual(before,self.store.items)
        with self.assertRaises(Conflict):self.reviews.save(PK.replace('#customer#','#foreign#'),'RICE',3,0,original,0)
        with self.assertRaises(ValueError):self.reviews.current(PK.replace('#COMBI_MEAL#','#JOB#'),'RICE')
        with self.assertRaises(ValueError):validate_combi_review({**original,'schema_version':'recipe-review-v1'})

    def dynamo(self):
        store=object.__new__(DynamoCookingStore);store.table=MagicMock();store.table.name='cooking'
        store.client=MagicMock();store.serializer=TypeSerializer();store.auth_table_name='auth';store.require_principal=True
        class Canceled(ClientError):pass
        store.client.exceptions.TransactionCanceledException=Canceled
        return store,Canceled

    def test_dynamo_atomic_auth_meta_pointer_and_immutable_review_contract(self):
        store,_=self.dynamo();value=record()
        self.assertEqual(CombiReviewStore(store).save(PK,'RICE',3,0,value,7),value)
        actions=store.client.transact_write_items.call_args.kwargs['TransactItems']
        model=botocore.session.get_session().get_service_model('dynamodb').operation_model('TransactWriteItems')
        validate_parameters({'TransactItems':actions},model.input_shape)
        self.assertEqual(len(actions),4)
        auth,meta,pointer,saved=[next(iter(a.values())) for a in actions]
        self.assertEqual(auth['Key'],{'pk':{'S':'USER#customer'}})
        self.assertEqual(auth['ExpressionAttributeValues'][':expected'],{'N':'7'})
        self.assertIn('deletion_lock_request',auth['ConditionExpression']);self.assertIn('suspended',auth['ConditionExpression'])
        self.assertIn('meal_batch_id',meta['ConditionExpression']);self.assertEqual(meta['ExpressionAttributeNames']['#job'],'rice_cooking_job_id')
        self.assertEqual(pointer['Key']['sk'],{'S':_keys('RICE')[0]})
        self.assertEqual(saved['Item']['sk'],{'S':_keys('RICE')[1]+f'{1:019d}'})
        self.assertTrue(all(next(iter(a.values()))['ReturnValuesOnConditionCheckFailure']=='NONE' for a in actions))
        self.assertEqual(set(a for action in actions for a in action),{'ConditionCheck','Put','Update'})
        self.assertEqual(sum('Put' in a for a in actions),1)  # No candidate/meal/recipe mutation.

    def test_failed_auth_or_meal_conditions_never_recover_duplicate_as_success(self):
        for failed in (0,1):
            store,Canceled=self.dynamo();reasons=[{'Code':'None'} for _ in range(4)]
            reasons[failed]={'Code':'ConditionalCheckFailed'};reasons[2]={'Code':'ConditionalCheckFailed'}
            store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},
                'CancellationReasons':reasons},'TransactWriteItems')
            with self.assertRaises(Conflict):CombiReviewStore(store).save(PK,'RICE',3,0,record(),0)
            store.table.get_item.assert_not_called()

    def test_duplicate_only_recovers_exact_record_but_capacity_or_missing_principal_fail_closed(self):
        store,Canceled=self.dynamo();value=record();sk=_keys('RICE')[1]+f'{1:019d}'
        store.table.get_item.return_value={'Item':{'pk':PK,'sk':sk,**value}}
        store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'None'},{'Code':'None'},{'Code':'ConditionalCheckFailed'},{'Code':'ConditionalCheckFailed'}]},'TransactWriteItems')
        self.assertEqual(CombiReviewStore(store).save(PK,'RICE',3,0,value,0),value)
        with self.assertRaises(Conflict):CombiReviewStore(store).save(PK,'RICE',3,0,{**value,'reason':'다른 내용'},0)
        store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'ProvisionedThroughputExceeded'}]},'TransactWriteItems')
        with self.assertRaises(Canceled):CombiReviewStore(store).save(PK,'RICE',3,0,value,0)
        store.require_principal=False
        with self.assertRaises(ValueError):CombiReviewStore(store).save(PK,'RICE',3,0,value,0)

    def test_dynamo_history_limit_consistency_cursor_and_corruption(self):
        store,_=self.dynamo();value=record();sk=_keys('RICE')[1]+f'{1:019d}'
        store.table.query.return_value={'Items':[{'pk':PK,'sk':sk,**value}],'LastEvaluatedKey':{'pk':PK,'sk':sk}}
        page=CombiReviewStore(store).history(PK,'RICE',1)
        self.assertEqual(page['next_cursor'],1)
        args=store.table.query.call_args.kwargs
        self.assertTrue(args['ConsistentRead']);self.assertEqual(args['Limit'],1);self.assertTrue(args['ScanIndexForward'])
        with self.assertRaises(ValueError):CombiReviewStore(store).history(PK,'RICE',1,1)
        store.table.query.return_value={'Items':[],'LastEvaluatedKey':{'pk':PK,'sk':sk}}
        with self.assertRaises(ValueError):CombiReviewStore(store).history(PK,'RICE',1)
