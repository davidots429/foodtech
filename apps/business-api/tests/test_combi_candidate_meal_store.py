"""New owned meals are created once; retry reads never reset later cooking."""
import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import MagicMock
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
import botocore.session
import test_combi_candidate_meal_plan as plans
from foodtech_business_api.combi_candidate_meal import CandidateMealRequest, candidate_meal_records
from foodtech_business_api.combi_candidate_meal_store import CombiCandidateMealStore
from foodtech_business_api.combi_recipe_candidate import CombiCandidateStore
from foodtech_business_api.combi_review_store import CombiReviewStore, _keys
from foodtech_business_api.cooking_store import MemoryCookingStore, DynamoCookingStore, Conflict, _meal_listing

NOW='2026-11-28T02:30:00Z'
SUBJECT='customer'


class CombiCandidateMealStoreTests(unittest.TestCase):
    def setUp(self):
        self.setup_menu('RICE')

    def setup_menu(self,menu):
        payload,candidate,review=plans.plan_input(menu)
        self.body=CandidateMealRequest(**payload);self.candidate=candidate;self.review=review
        self.source=f'SCOPE#USER#{SUBJECT}#COMBI_MEAL#{payload["meal_id"]}'
        self.target=f'SCOPE#USER#{SUBJECT}#COMBI_MEAL#{payload["request_id"]}'
        self.history=f'SCOPE#USER#{SUBJECT}#COMBI_MEALS'
        self.store=MemoryCookingStore()
        meta={'pk':self.source,'sk':'META','meal_batch_id':payload['meal_id'],'version':3,'provenance':'SYNTHETIC',
            ('rice_cooking_job_id' if menu=='RICE' else 'mackerel_cooking_job_id'):review['job_id'],
            'equipment_size':'MEDIUM_FIXED_RACK_10'}
        self.store.items[self.source,'META']=meta
        self.store.items[self.history,'HISTORY#REVISION']={'pk':self.history,'sk':'HISTORY#REVISION','revision':7}
        CombiReviewStore(self.store).save(self.source,menu,3,0,review,0)
        self.assertEqual(CombiCandidateStore(self.store).preserve(self.source,menu,1,3,review['proposal_digest'],candidate['recorded_at'],0),candidate)
        self.adapter=CombiCandidateMealStore(self.store)

    def test_new_rows_listing_and_counter_once_source_unchanged_for_both_menus(self):
        for menu in ('RICE','MACKEREL'):
            self.setup_menu(menu);before=copy.deepcopy(self.store.items)
            self.assertTrue(self.adapter.create(SUBJECT,1,self.body,NOW,0))
            rows=self.store.read_meal(self.target);self.assertEqual(len(rows),4)
            meta=next(r for r in rows if r['sk']=='META')
            self.assertEqual(meta['version'],1);self.assertEqual(meta['status'],'PREPARING_RICE')
            self.assertEqual(meta['recipe_candidate_selection']['candidate'],self.candidate)
            for row in rows:
                if row['sk'].startswith('JOB#'):
                    self.assertEqual(row['safety_status'],'NOT_MEASURED');self.assertEqual(row['status'],'PENDING')
                    self.assertNotIn('products',row);self.assertNotIn('monitored_process_source',row)
            self.assertEqual(self.store.items[self.history,'HISTORY#REVISION']['revision'],8)
            self.assertIn((self.history,'MEAL#'+str(self.body.request_id)),self.store.items)
            for key,row in before.items():
                if key!=(self.history,'HISTORY#REVISION'):self.assertEqual(self.store.items[key],row)
            snapshot=copy.deepcopy(self.store.items)
            self.assertFalse(self.adapter.create(SUBJECT,1,self.body,'2026-11-28T03:00:00Z',0))
            self.assertEqual(snapshot,self.store.items)

    def test_recovery_after_target_advances_and_old_source_changes_does_not_reset_or_increment(self):
        self.adapter.create(SUBJECT,1,self.body,NOW,0)
        self.store.items[self.target,'META'].update(version=4,status='RICE_COOKING',last_observed_at=NOW)
        rice=next(row for row in self.store.read_meal(self.target) if row.get('menu')=='RICE')
        self.store.items[self.target,rice['sk']].update(status='RUNNING',safety_status='SUSPECTED')
        self.store.items[self.source,'META']['version']=4
        self.store.items[self.source,_keys('RICE')[0]]['version']=2
        snapshot=copy.deepcopy(self.store.items)
        self.assertFalse(self.adapter.create(SUBJECT,1,self.body,'2026-11-28T03:00:00Z',0))
        self.assertEqual(self.store.items,snapshot)

    def test_different_request_same_id_corrupt_selection_or_missing_linked_row_cannot_recover(self):
        self.adapter.create(SUBJECT,1,self.body,NOW,0)
        different=self.body.model_copy(deep=True);different.new_meal.added_water_kg+=1
        with self.assertRaises(Conflict):self.adapter.create(SUBJECT,1,different,NOW,0)
        snapshot=copy.deepcopy(self.store.items)
        self.store.items[self.target,'META']['recipe_candidate_selection']['request_hash']='0'*64
        with self.assertRaises(Conflict):self.adapter.create(SUBJECT,1,self.body,NOW,0)
        self.store.items=copy.deepcopy(snapshot)
        child=next(key for key in self.store.items if key[0]==self.target and key[1].startswith('TRANSITION#'))
        del self.store.items[child]
        with self.assertRaises(Conflict):self.adapter.create(SUBJECT,1,self.body,NOW,0)

    def test_current_source_rejection_or_changed_profile_blocks_fresh_create(self):
        for mutate in ('pointer','version','profile','base'):
            self.setUp()
            if mutate=='pointer':self.store.items[self.source,_keys('RICE')[0]]['version']=2
            if mutate=='version':self.store.items[self.source,'META']['version']=4
            if mutate=='profile':self.store.items[self.source,'META']['equipment_size']='LARGE_ROLL_IN_RACK_20'
            if mutate=='base':self.store.items[self.source,'META']['rice_recipe_version']='changed'
            before=copy.deepcopy(self.store.items)
            with self.assertRaises((Conflict,ValueError)):self.adapter.create(SUBJECT,1,self.body,NOW,0)
            self.assertEqual(before,self.store.items)

    def test_same_request_concurrency_creates_one_meal_and_one_history_increment(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.adapter.create(SUBJECT,1,self.body,NOW,0),range(2)))
        self.assertCountEqual(results,[True,False])
        self.assertEqual(self.store.items[self.history,'HISTORY#REVISION']['revision'],8)
        self.assertEqual(len(self.store.read_meal(self.target)),4)

    def dynamo(self):
        store=object.__new__(DynamoCookingStore);store.table=MagicMock();store.table.name='cooking'
        store.client=MagicMock();store.serializer=TypeSerializer();store.auth_table_name='auth';store.require_principal=True
        class Canceled(ClientError):pass
        store.client.exceptions.TransactionCanceledException=Canceled
        rows=copy.deepcopy(self.store.items)
        store.table.get_item.side_effect=lambda **args:{'Item':copy.deepcopy(rows.get((args['Key']['pk'],args['Key']['sk'])))}
        return store,Canceled,rows

    def test_dynamo_eleven_action_atomic_auth_source_candidate_new_records_listing_history_shape(self):
        store,_,_=self.dynamo();self.assertTrue(CombiCandidateMealStore(store).create(SUBJECT,1,self.body,NOW,9))
        actions=store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(actions),11)
        model=botocore.session.get_session().get_service_model('dynamodb').operation_model('TransactWriteItems')
        validate_parameters({'TransactItems':actions},model.input_shape)
        values=[next(iter(a.values())) for a in actions]
        self.assertEqual([list(a)[0] for a in actions],['ConditionCheck']*5+['Put']*5+['Update'])
        self.assertEqual(values[0]['Key'],{'pk':{'S':'USER#customer'}})
        self.assertIn('deletion_lock_request',values[0]['ConditionExpression'])
        self.assertEqual(values[0]['ExpressionAttributeValues'][':expected'],{'N':'9'})
        self.assertIn('attribute_not_exists(#absent0)',values[1]['ConditionExpression'])
        self.assertIn('rice_recipe_version',values[1]['ExpressionAttributeNames'].values())
        self.assertEqual(set(values[3]['ExpressionAttributeNames'].values()),set(self.review))
        self.assertEqual(set(values[4]['ExpressionAttributeNames'].values()),set(self.candidate))
        self.assertEqual(values[10]['Key'],{'pk':{'S':self.history},'sk':{'S':'HISTORY#REVISION'}})
        keys=[]
        for value in values:
            key=value.get('Key',value.get('Item'))
            keys.append((value['TableName'],str(key['pk']),str(key.get('sk'))))
        self.assertEqual(len(set(keys)),11)
        self.assertTrue(all(v['ReturnValuesOnConditionCheckFailure']=='NONE' for v in values))
        self.assertTrue(all(v['ConditionExpression']=='attribute_not_exists(pk)' for v in values[5:10]))

    def test_dynamo_auth_source_condition_failure_never_becomes_successful_duplicate(self):
        for failed in range(5):
            store,Canceled,rows=self.dynamo();reasons=[{'Code':'None'} for _ in range(11)]
            reasons[failed]={'Code':'ConditionalCheckFailed'}
            store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},'CancellationReasons':reasons},'TransactWriteItems')
            with self.assertRaises(Conflict):CombiCandidateMealStore(store).create(SUBJECT,1,self.body,NOW,0)
            target_reads=[call for call in store.table.get_item.call_args_list if call.kwargs['Key']['pk']==self.target]
            self.assertEqual(len(target_reads),1)  # No post-denial recovery lookup.

    def test_dynamo_duplicate_and_lost_response_recovery_do_not_repeat_counter_or_reset(self):
        store,Canceled,rows=self.dynamo()
        def duplicate(**args):
            expected=candidate_meal_records(self.body,self.candidate,self.review,NOW)
            for row in expected:rows[self.target,row['sk']]={'pk':self.target,**row}
            listing=_meal_listing(self.target,expected);rows[listing['pk'],listing['sk']]=listing
            raise Canceled({'Error':{'Code':'TransactionCanceledException'},'CancellationReasons':
                [{'Code':'None'}]*5+[{'Code':'ConditionalCheckFailed'}]*5+[{'Code':'None'}]},'TransactWriteItems')
        store.client.transact_write_items.side_effect=duplicate
        self.assertFalse(CombiCandidateMealStore(store).create(SUBJECT,1,self.body,NOW,0))
        self.assertEqual(store.client.transact_write_items.call_count,1)
        rows[self.target,'META'].update(version=8,status='MACKEREL_UNLOADED')
        rows[self.source,'META']['version']=9
        self.assertFalse(CombiCandidateMealStore(store).create(SUBJECT,1,self.body,'2026-11-28T03:00:00Z',0))
        self.assertEqual(store.client.transact_write_items.call_count,1)
        self.assertEqual(rows[self.target,'META']['version'],8)

    def test_capacity_errors_and_missing_shared_principal_fail_without_recovery(self):
        store,Canceled,_=self.dynamo()
        store.client.transact_write_items.side_effect=Canceled({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'ProvisionedThroughputExceeded'}]},'TransactWriteItems')
        with self.assertRaises(Canceled):CombiCandidateMealStore(store).create(SUBJECT,1,self.body,NOW,0)
        store.require_principal=False;store.table.get_item.reset_mock()
        with self.assertRaises(ValueError):CombiCandidateMealStore(store).create(SUBJECT,1,self.body,NOW,0)
        store.table.get_item.assert_not_called()
