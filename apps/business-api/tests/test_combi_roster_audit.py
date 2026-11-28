"""Bounded retained-key comparison is an observation, not writer enforcement."""
import copy
from decimal import Decimal
from types import SimpleNamespace
import unittest
from uuid import uuid5,NAMESPACE_DNS
from boto3.dynamodb.types import TypeSerializer,TypeDeserializer
import botocore.session
from botocore.validate import validate_parameters
from foodtech_business_api.combi_roster_audit import DynamoCombiRosterAudit
from foodtech_business_api.combi_meal import CreateMeal,new_meal_records
from foodtech_contracts.combi_history_witness import roster_digest

OWNER='private-audit-owner'


class Paginator:
    def __init__(self,pages,requests):self.pages=pages;self.requests=requests
    def paginate(self,**kwargs):self.requests.append(kwargs);return iter(copy.deepcopy(self.pages))


class AuditStore:
    def __init__(self,count=2):
        self.items={};self.ids=[];self.requests=[];self.page_requests=[];self.revisions=[3,3]
        self.table=SimpleNamespace(name='foodtech-cooking-jobs-dev',meta=SimpleNamespace(client=self))
        self.client=self
        self.serializer=TypeSerializer();self.decoder=TypeDeserializer()
        body=CreateMeal(equipment_size='MEDIUM_FIXED_RACK_10',rack_count=1,pan_count=1,
            dry_rice_kg='2',drained_rice_kg='2.4',added_water_kg='2',pan_tare_kg='1',rack_tare_kg='0')
        for n in range(count):
            ids=tuple(str(uuid5(NAMESPACE_DNS,f'combi-audit-{n}-{name}')) for name in ('meal','rice','transition','fish'))
            self.ids.append(ids[0]);pk=f'SCOPE#USER#{OWNER}#COMBI_MEAL#{ids[0]}'
            for row in new_meal_records(body,ids,'2026-11-28T01:00:00Z'):self.items[pk,row['sk']]={'pk':pk,**row}
        self.ids.sort();self.refresh_pages()

    def refresh_pages(self):
        keys=[{'pk':pk,'sk':sk} for pk,sk in sorted(self.items)]
        self.scan=[]
        for offset in range(0,len(keys),1000):
            page={'Items':keys[offset:offset+1000],'ScannedCount':len(keys[offset:offset+1000])}
            if offset+1000<len(keys):page['LastEvaluatedKey']=keys[offset+999]
            self.scan.append(page)
        if not self.scan:self.scan=[{'Items':[],'ScannedCount':0}]
        pk=f'SCOPE#USER#{OWNER}#COMBI_MEALS'
        rows=[{'pk':pk,'sk':'MEAL#'+meal,'meal_batch_id':meal} for meal in self.ids];self.listing=[]
        for offset in range(0,len(rows),50):
            page={'Items':rows[offset:offset+50]}
            if offset+50<len(rows):page['LastEvaluatedKey']={k:rows[offset+49][k] for k in ('pk','sk')}
            self.listing.append(page)
        if not self.listing:self.listing=[{'Items':[]}]

    def get_paginator(self,name):return Paginator(self.scan if name=='scan' else self.listing,self.page_requests)
    def meal_history_revision(self,owner):
        if owner!=OWNER:raise ValueError
        return self.revisions.pop(0)
    def transact_get_items(self,**kwargs):
        self.requests.append(copy.deepcopy(kwargs));result=[]
        for entry in kwargs['TransactItems']:
            key={k:self.decoder.deserialize(v) for k,v in entry['Get']['Key'].items()}
            row=self.items.get((key['pk'],key['sk']))
            result.append({'Item':{k:self.serializer.serialize(v) for k,v in row.items()}} if row else {})
        return {'Responses':result}


class CombiRosterAuditTests(unittest.TestCase):
    def audit(self,store,**kwargs):return DynamoCombiRosterAudit(store=store).observe(owner=OWNER,**kwargs)

    def test_full_roster_and_all_linked_keys_are_matched_without_issuing_writer_controls(self):
        store=AuditStore();result=self.audit(store)
        self.assertEqual(result.count,2);self.assertEqual(result.revision,3);self.assertTrue(result.revision_present)
        self.assertEqual(result.roster_sha256,roster_digest(store.ids));self.assertNotIn(OWNER,repr(result))
        self.assertNotIn('ACTIVE',repr(result));self.assertFalse(hasattr(store,'transact_write_items'))
        shape=botocore.session.get_session().get_service_model('dynamodb').operation_model('TransactGetItems').input_shape
        for request in store.requests:
            validate_parameters(request,shape)
            keys=[repr(entry['Get']['Key']) for entry in request['TransactItems']]
            self.assertEqual(len(set(keys)),len(keys));self.assertLessEqual(len(keys),50)
            for entry in request['TransactItems']:
                self.assertNotIn('rice_preparation',entry['Get']['ProjectionExpression'])
                self.assertNotIn('recipe_candidate_selection',entry['Get']['ProjectionExpression'])
        self.assertTrue(all(r['ConsistentRead'] for r in store.page_requests))

    def test_1001_meals_read_every_page_and_every_linked_record(self):
        store=AuditStore(1001);result=self.audit(store)
        self.assertEqual(result.count,1001)
        self.assertEqual(sum(len(r['TransactItems']) for r in store.requests),4004)
        self.assertEqual(result.roster_sha256,roster_digest(store.ids))

    def test_empty_baseline_is_zero_only_when_both_retained_records_and_counter_are_absent(self):
        store=AuditStore(0);store.revisions=[None,None]
        result=self.audit(store);self.assertEqual((result.count,result.revision,result.revision_present),(0,0,False))
        self.assertEqual(result.roster_sha256,roster_digest([]));self.assertEqual(store.requests,[])
        store=AuditStore();store.revisions=[None,None]
        with self.assertRaises(ValueError):self.audit(store)
        store=AuditStore(0);store.revisions=[4,4]
        self.assertEqual(self.audit(store).revision,4)

    def test_missing_dangling_or_physically_mismatched_listing_is_rejected(self):
        for change in ('missing','dangling','wrong-sk'):
            store=AuditStore()
            if change=='missing':store.listing[0]['Items'].pop()
            elif change=='dangling':
                meal=str(uuid5(NAMESPACE_DNS,'dangling'));store.listing[0]['Items'].append({
                    'pk':f'SCOPE#USER#{OWNER}#COMBI_MEALS','sk':'MEAL#'+meal,'meal_batch_id':meal})
                store.listing[0]['Items'].sort(key=lambda r:r['sk'])
            else:store.listing[0]['Items'][0]['sk']='MEAL#'+store.ids[1]
            with self.assertRaises(ValueError):self.audit(store)

    def test_live_source_and_event_children_require_a_real_parent_without_claiming_payload_completeness(self):
        store=AuditStore();pk=f'SCOPE#USER#{OWNER}#COMBI_MEAL#{store.ids[0]}'
        for suffix,sk in (('#LIVE','META'),('#SOURCE','RAW#0000'),('','EVENT#000000000002')):
            store.items[pk+suffix,sk]={'pk':pk+suffix,'sk':sk}
        store.refresh_pages();self.assertEqual(self.audit(store).count,2)
        for suffix,sk in (('#LIVE','META'),('#SOURCE','RAW#0000'),('','EVENT#000000000002')):
            store=AuditStore(0);store.revisions=[None,None]
            store.items[pk+suffix,sk]={'pk':pk+suffix,'sk':sk};store.refresh_pages()
            with self.assertRaises(ValueError):self.audit(store)

    def test_missing_extra_or_wrongly_owned_child_cannot_be_covered(self):
        for change in ('missing','extra','wrong-parent','wrong-job'):
            store=AuditStore();pk=f'SCOPE#USER#{OWNER}#COMBI_MEAL#{store.ids[0]}'
            key=next(k for k in store.items if k[0]==pk and k[1].startswith('JOB#RICE'))
            if change=='missing':del store.items[key]
            elif change=='extra':store.items[pk,'JOB#UNKNOWN']={'pk':pk,'sk':'JOB#UNKNOWN'}
            elif change=='wrong-parent':store.items[key]['meal_batch_id']=store.ids[1]
            else:store.items[key]['job_id']=str(uuid5(NAMESPACE_DNS,'wrong-job'))
            store.refresh_pages()
            with self.assertRaises(ValueError):self.audit(store)

    def test_revision_change_during_scan_or_metadata_read_is_rejected(self):
        store=AuditStore();store.revisions=[3,4]
        with self.assertRaises(ValueError):self.audit(store)
        for value in (0,True,Decimal('1.5')):
            store=AuditStore();store.revisions=[value,value]
            with self.assertRaises(ValueError):self.audit(store)

    def test_scan_limit_bad_accounting_repeated_cursor_or_unobserved_completion_are_rejected(self):
        for change in ('limit','count','repeat','incomplete','foreign','trailing-pk'):
            store=AuditStore();kwargs={}
            if change=='limit':kwargs={'max_scanned':1}
            elif change=='count':store.scan[0]['ScannedCount']=True
            elif change=='repeat':
                store.scan[0]['LastEvaluatedKey']={'pk':'cursor','sk':'same'};store.scan*=2
            elif change=='incomplete':store.scan[0]['LastEvaluatedKey']={'pk':'cursor','sk':'last'}
            elif change=='foreign':store.scan[0]['Items'][0]['pk']='SCOPE#USER#foreign#COMBI_MEAL#'+store.ids[0]
            else:store.scan[0]['Items'][0]['pk']+='#'
            with self.assertRaises(ValueError):self.audit(store,**kwargs)

    def test_invalid_scope_or_table_never_starts_a_scan(self):
        store=AuditStore();audit=DynamoCombiRosterAudit(store=store)
        for owner,limit in [('a#b',1000),(OWNER,True),(OWNER,0),(OWNER,1000001)]:
            with self.assertRaises(ValueError):audit.observe(owner=owner,max_scanned=limit)
        self.assertEqual(store.page_requests,[])
        store.table.name='unrelated'
        with self.assertRaises(ValueError):DynamoCombiRosterAudit(store=store)

    def test_resource_paginator_and_low_level_transactions_use_real_sdk_marshalling_without_network(self):
        import boto3
        from botocore.stub import Stubber
        from foodtech_business_api.cooking_store import DynamoCookingStore
        fixture=AuditStore();encode=lambda row:{k:fixture.serializer.serialize(v) for k,v in row.items()}
        session=boto3.Session(aws_access_key_id='synthetic',aws_secret_access_key='synthetic',region_name='ap-southeast-2')
        resource=session.resource('dynamodb');client=session.client('dynamodb')
        store=object.__new__(DynamoCookingStore);store.table=resource.Table(fixture.table.name);store.client=client
        meta=[fixture.items[f'SCOPE#USER#{OWNER}#COMBI_MEAL#{meal}','META'] for meal in fixture.ids]
        child=[]
        for meal in fixture.ids:
            pk=f'SCOPE#USER#{OWNER}#COMBI_MEAL#{meal}'
            child.extend(row for (p,sk),row in sorted(fixture.items.items()) if p==pk and sk!='META')
        with Stubber(resource.meta.client) as native,Stubber(client) as low:
            native.add_response('get_item',{'Item':encode({'pk':f'SCOPE#USER#{OWNER}#COMBI_MEALS','sk':'HISTORY#REVISION','revision':3})})
            native.add_response('scan',{'Items':[encode(row) for row in fixture.scan[0]['Items']],
                'ScannedCount':8,'Count':8})
            native.add_response('query',{'Items':[encode(row) for row in fixture.listing[0]['Items']],
                'Count':2,'ScannedCount':2})
            native.add_response('get_item',{'Item':encode({'pk':f'SCOPE#USER#{OWNER}#COMBI_MEALS','sk':'HISTORY#REVISION','revision':3})})
            low.add_response('transact_get_items',{'Responses':[{'Item':encode(row)} for row in meta]})
            low.add_response('transact_get_items',{'Responses':[{'Item':encode(row)} for row in child]})
            result=DynamoCombiRosterAudit(store=store).observe(owner=OWNER)
            self.assertEqual(result.count,2)
            native.assert_no_pending_responses();low.assert_no_pending_responses()

    def test_evaluated_item_budget_refuses_next_page_before_spending_remaining_partial_page(self):
        store=AuditStore(0)
        store.scan=[{'Items':[],'ScannedCount':1000,'LastEvaluatedKey':{'pk':'cursor','sk':'one'}},
                    {'Items':[],'ScannedCount':1}]
        with self.assertRaises(ValueError):self.audit(store,max_scanned=1001)

    def test_listing_repeated_empty_cursor_or_missing_terminal_page_is_rejected(self):
        for kind in ('repeat','incomplete'):
            store=AuditStore(0)
            page={'Items':[],'LastEvaluatedKey':{'pk':f'SCOPE#USER#{OWNER}#COMBI_MEALS','sk':'MEAL#'+'00000000-0000-4000-8000-000000000001'}}
            store.listing=[page,page] if kind=='repeat' else [page]
            with self.assertRaises(ValueError):self.audit(store)
