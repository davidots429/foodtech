"""Actual bounded audit plus mocked protected-control CAS and receipt recovery."""
import copy
import unittest
from uuid import uuid4
from boto3.dynamodb.types import TypeSerializer,TypeDeserializer
from botocore.exceptions import ClientError
import botocore.session
from botocore.validate import validate_parameters
from test_combi_roster_audit import AuditStore,OWNER
from foodtech_business_api.combi_history_coverage import DynamoCombiHistoryCoverageRegistrar,CombiHistoryCoverageUnavailable
from foodtech_contracts.combi_history_witness import CONTROL_KEY,COVERAGE_FIELD,HISTORY_CONTRACT,COVERAGE_SCOPE,roster_digest


class ControlClient:
    def __init__(self,store):
        self.store=store;self.user={'pk':'USER#'+OWNER,'epoch':2,'suspended':False,'history_coverage':{'private':'unchanged'}}
        self.writer={'pk':CONTROL_KEY,'state':'ENFORCED','version':1,'generation':str(uuid4()),'writer_contract':HISTORY_CONTRACT,
            'cooking_table_arn':'arn:aws:dynamodb:ap-southeast-2:123456789012:table/'+store.table.name,
            'manifest_sha256':'a'*64,'verified_at':900,'valid_until':1100}
        self.writes=[];self.reads=[];self.count=0;self.on_read=None;self.after_write=None;self.failure=None;self.lost=False
        self.serializer=TypeSerializer();self.decoder=TypeDeserializer()
    def transact_get_items(self,**kwargs):
        self.reads.append(copy.deepcopy(kwargs))
        if kwargs['TransactItems'][0]['Get']['TableName']=='foodtech-auth-sessions-dev':
            self.count+=1
            if self.on_read:self.on_read(self.count)
            return {'Responses':[{'Item':{k:self.serializer.serialize(v) for k,v in row.items()}} for row in (self.user,self.writer)]}
        return self.store.transact_get_items(**kwargs)
    def transact_write_items(self,**kwargs):
        self.writes.append(copy.deepcopy(kwargs))
        if self.failure:raise self.failure
        self.user[COVERAGE_FIELD]=self.decoder.deserialize(kwargs['TransactItems'][0]['Update']['ExpressionAttributeValues'][':coverage'])
        if self.after_write:self.after_write()
        if self.lost:raise ClientError({'Error':{'Code':'ServiceUnavailable','Message':'private-marker'}},'TransactWriteItems')
        return {}


class CombiHistoryCoverageTests(unittest.TestCase):
    def setUp(self):
        self.store=AuditStore();self.client=ControlClient(self.store);self.store.client=self.client;self.now=1000
        self.registrar=DynamoCombiHistoryCoverageRegistrar(auth_table='foodtech-auth-sessions-dev',
            cooking_table=self.store.table.name,account_id='123456789012',client=self.client,store=self.store,clock=lambda:self.now)
        self.operation=str(uuid4())
    def register(self):return self.registrar.register(owner=OWNER,auth_epoch=2,registration_id=self.operation)
    def recover(self):return self.registrar.recover(owner=OWNER,auth_epoch=2,registration_id=self.operation)

    def test_real_bounded_audit_is_cas_bound_to_account_writer_history_and_post_write_receipt(self):
        result=self.register()
        self.assertEqual(result,{'coverage_version':1,'record_count':2,'receipt_only':True,'immediately_previous_verified':False})
        self.assertEqual(self.client.user['history_coverage'],{'private':'unchanged'})
        self.assertEqual(len(self.client.writes),1);request=self.client.writes[0]
        shape=botocore.session.get_session().get_service_model('dynamodb').operation_model('TransactWriteItems').input_shape
        validate_parameters(request,shape)
        update,writer,history=request['TransactItems']
        self.assertIn('attribute_exists(pk)',update['Update']['ConditionExpression'])
        self.assertIn('attribute_not_exists(#coverage)',update['Update']['ConditionExpression'])
        self.assertEqual(update['Update']['UpdateExpression'],'SET #coverage = :coverage')
        self.assertEqual(update['Update']['ExpressionAttributeNames']['#coverage'],COVERAGE_FIELD)
        self.assertEqual(self.client.decoder.deserialize(writer['ConditionCheck']['Key']['pk']),CONTROL_KEY)
        self.assertIn('valid_until > :now',writer['ConditionCheck']['ConditionExpression'])
        self.assertEqual(self.client.decoder.deserialize(history['ConditionCheck']['Key']['pk']),f'SCOPE#USER#{OWNER}#COMBI_MEALS')
        self.assertEqual(history['ConditionCheck']['ExpressionAttributeValues'],{':revision':{'N':'3'}})
        for action in request['TransactItems']:self.assertEqual(next(iter(action.values()))['ReturnValuesOnConditionCheckFailure'],'NONE')
        coverage=self.client.user[COVERAGE_FIELD]
        self.assertEqual(coverage['coverage_scope'],COVERAGE_SCOPE)
        self.assertEqual(coverage['baseline_roster_sha256'],roster_digest(self.store.ids))
        self.assertEqual(coverage['registration_id'],self.operation)
        self.assertEqual(self.client.count,3)
        self.assertNotIn(OWNER,repr(self.registrar));self.assertNotIn(OWNER,repr(result))

    def test_missing_or_invalid_independent_writer_account_or_clock_prevents_any_scan_and_write(self):
        changes=[('writer','state','UNVERIFIED'),('writer','valid_until',1000),('writer','generation','bad'),
            ('writer','manifest_sha256','bad'),('writer','writer_contract','old'),('writer','version',True),
            ('writer','cooking_table_arn','foreign'),('user','epoch',1),('user','suspended',True),
            ('user','deletion_lock_request',str(uuid4())),('user','pk','USER#foreign')]
        for target,key,value in changes:
            self.setUp();getattr(self.client,target)[key]=value
            with self.assertRaises(CombiHistoryCoverageUnavailable):self.register()
            self.assertEqual(self.store.page_requests,[]);self.assertEqual(self.client.writes,[])
        self.setUp();self.now=1000.0
        with self.assertRaises(CombiHistoryCoverageUnavailable):self.register()
        self.assertEqual(self.client.writes,[])

    def test_account_writer_or_revision_change_during_audit_cannot_register(self):
        for kind in ('epoch','writer','revision'):
            self.setUp()
            if kind=='revision':self.store.revisions=[3,4]
            else:
                def changed(count):
                    if count==2:
                        if kind=='epoch':self.client.user['epoch']=3
                        else:self.client.writer['manifest_sha256']='b'*64
                self.client.on_read=changed
            with self.assertRaises(CombiHistoryCoverageUnavailable):self.register()
            self.assertEqual(self.client.writes,[])

    def test_empty_coverage_fences_absent_counter_without_creating_it(self):
        self.store=AuditStore(0);self.store.revisions=[None,None]
        self.client=ControlClient(self.store);self.store.client=self.client
        self.registrar=DynamoCombiHistoryCoverageRegistrar(auth_table='foodtech-auth-sessions-dev',cooking_table=self.store.table.name,
            account_id='123456789012',client=self.client,store=self.store,clock=lambda:1000)
        self.assertEqual(self.register()['record_count'],0)
        history=self.client.writes[0]['TransactItems'][2]['ConditionCheck']
        self.assertEqual(history['ConditionExpression'],'attribute_not_exists(pk)')
        self.assertNotIn('ExpressionAttributeValues',history)
        coverage=self.client.user[COVERAGE_FIELD]
        self.assertEqual(coverage['coverage_revision'],0);self.assertEqual(coverage['baseline_roster_sha256'],roster_digest([]))

    def test_replacement_uses_exact_old_coverage_not_unconditional_overwrite(self):
        old={'version':4,'state':'STALE'};self.client.user[COVERAGE_FIELD]=old
        self.assertEqual(self.register()['coverage_version'],5)
        update=self.client.writes[0]['TransactItems'][0]['Update']
        self.assertIn('#coverage = :old',update['ConditionExpression'])
        self.assertEqual(self.client.decoder.deserialize(update['ExpressionAttributeValues'][':old']),old)

    def test_transaction_failure_is_not_retried_or_exposed_as_success(self):
        for code in ('TransactionCanceledException','ProvisionedThroughputExceededException'):
            self.setUp();self.client.failure=ClientError({'Error':{'Code':code,'Message':'private-marker'}},'TransactWriteItems')
            with self.assertRaises(CombiHistoryCoverageUnavailable) as caught:self.register()
            self.assertNotIn('private-marker',str(caught.exception));self.assertEqual(len(self.client.writes),1)
            self.assertNotIn(COVERAGE_FIELD,self.client.user)

    def test_lost_response_recovers_only_same_operation_without_new_audit_or_write(self):
        self.client.lost=True
        with self.assertRaises(CombiHistoryCoverageUnavailable):self.register()
        scans=len(self.store.page_requests);writes=len(self.client.writes)
        recovered=self.recover();self.assertEqual(recovered['coverage_version'],1)
        self.assertEqual((len(self.store.page_requests),len(self.client.writes)),(scans,writes))
        with self.assertRaises(CombiHistoryCoverageUnavailable):self.registrar.recover(owner=OWNER,auth_epoch=2,registration_id=str(uuid4()))
        with self.assertRaises(CombiHistoryCoverageUnavailable):self.register()
        self.assertEqual(len(self.client.writes),1)

    def test_post_write_expiry_or_replaced_receipt_is_unavailable_even_if_commit_happened(self):
        for kind in ('expiry','operation'):
            self.setUp()
            def changed():
                if kind=='expiry':self.now=1200
                else:self.client.user[COVERAGE_FIELD]['registration_id']=str(uuid4())
            self.client.after_write=changed
            with self.assertRaises(CombiHistoryCoverageUnavailable):self.register()
            self.assertIn(COVERAGE_FIELD,self.client.user);self.assertEqual(len(self.client.writes),1)

    def test_recovery_rejects_stale_malformed_revoked_or_cross_generation_receipts(self):
        self.register();original=copy.deepcopy(self.client.user)
        for key,value in [('coverage_revision',0),('record_count',0),('coverage_scope','ALL_RETAINED_JOBS'),
                          ('proof_sha256','bad'),('registration_epoch',3),('generation',str(uuid4()))]:
            self.client.user=copy.deepcopy(original);self.client.user[COVERAGE_FIELD][key]=value
            with self.assertRaises(CombiHistoryCoverageUnavailable):self.recover()
        self.client.user=copy.deepcopy(original);self.client.user['suspended']=True
        with self.assertRaises(CombiHistoryCoverageUnavailable):self.recover()

    def test_registered_receipt_is_consumed_by_existing_full_prior_history_authority(self):
        from datetime import datetime,timezone
        import hashlib
        from test_combi_prior_history import HistoryClient
        from foodtech_business_api.combi_prior_history import DynamoCombiPriorHistoryAuthority
        from foodtech_business_api.prior_history_authority import PriorHistoryUnavailable
        from foodtech_contracts.combi_execution_binding import CombiExecutionBinding
        self.now=int(datetime(2026,11,29,tzinfo=timezone.utc).timestamp())
        self.client.writer.update(verified_at=self.now-100,valid_until=self.now+100)
        prior,current=self.store.ids
        for day,meal in enumerate((prior,current),27):
            pk=f'SCOPE#USER#{OWNER}#COMBI_MEAL#{meal}';meta=self.store.items[pk,'META']
            meta['source']={'device_id':'synthetic-combi-device','raw_sha256':hashlib.sha256(meal.encode()).hexdigest()}
            rice=self.store.items[pk,'JOB#RICE#'+meta['rice_cooking_job_id']]
            rice.update(status='UNLOADED',active_cooking_started_at=f'2026-11-{day}T00:00:00Z',
                        food_unloaded_at=f'2026-11-{day}T00:30:00Z')
        self.register()
        reader=HistoryClient(OWNER);reader.items=copy.deepcopy(self.store.items)
        for row in (self.client.user,self.client.writer,
                    {'pk':f'SCOPE#USER#{OWNER}#COMBI_MEALS','sk':'HISTORY#REVISION','revision':3},
                    *self.store.listing[0]['Items']):
            reader.items[row['pk'],row.get('sk')]=copy.deepcopy(row)
        current_meta=self.store.items[f'SCOPE#USER#{OWNER}#COMBI_MEAL#{current}','META']
        prior_meta=self.store.items[f'SCOPE#USER#{OWNER}#COMBI_MEAL#{prior}','META']
        binding=CombiExecutionBinding('RICE',current,int(current_meta['version']),current_meta['rice_cooking_job_id'],
            current_meta['source']['raw_sha256'],prior,int(prior_meta['version']),prior_meta['rice_cooking_job_id'],
            prior_meta['source']['raw_sha256'],2)
        authority=DynamoCombiPriorHistoryAuthority(auth_table=self.registrar.auth,cooking_table=self.registrar.cooking,
            account_id='123456789012',client=reader,clock=lambda:self.now)
        witness=authority.observe(owner=OWNER,binding=binding)
        coverage=self.client.user[COVERAGE_FIELD]
        self.assertEqual(witness.coverage_proof_sha256,coverage['proof_sha256'])
        self.assertEqual(witness.baseline_roster_sha256,coverage['baseline_roster_sha256'])
        self.assertEqual(authority.revalidate(witness),witness)
        reader.items[CONTROL_KEY,None]['state']='UNVERIFIED'
        with self.assertRaises(PriorHistoryUnavailable):authority.observe(owner=OWNER,binding=binding)
        self.assertFalse(any(kind=='check' for kind,_ in reader.calls))
