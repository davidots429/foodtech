"""Mocked AWS coverage CAS, with actual bounded roster scan/list comparison."""
import copy
import json
import unittest
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
from botocore.stub import ANY, Stubber

from foodtech_business_api.history_coverage import DynamoHistoryCoverageRegistrar, HistoryCoverageUnavailable
from foodtech_business_api.prior_history_authority import DynamoPriorHistoryAuthority
from foodtech_business_api.roster_audit import audit_snapshot
from foodtech_contracts.prior_history_witness import HISTORY_CONTRACT
from test_roster_audit import FakeStore


class HistoryCoverageTests(unittest.TestCase):
    def setUp(self):
        self.owner = 'opaque_owner'
        self.ids = [str(uuid4()), str(uuid4())]
        self.store = FakeStore(self.owner, self.ids)
        self.client = boto3.client('dynamodb', region_name='ap-southeast-2',
            aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        self.registrar = DynamoHistoryCoverageRegistrar(auth_table='foodtech-auth-sessions-dev',
            cooking_table=self.store.table.name, account_id='123456789012', client=self.client, store=self.store, clock=lambda:1000)
        self.user = {'pk':'USER#'+self.owner, 'epoch':2, 'suspended':False}
        self.writer = {'pk':'CONTROL#COOKING_HISTORY_WRITERS','state':'ENFORCED','version':1,'generation':str(uuid4()),
            'writer_contract':HISTORY_CONTRACT,'cooking_table_arn':self.registrar.cooking_arn,
            'manifest_sha256':'a'*64,'verified_at':900,'valid_until':1100}
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()
        self.requests = []
        self.client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
            lambda params, **_: self.requests.append(copy.deepcopy(params)))

    def response(self, user=None, writer=None):
        return {'Responses':[{'Item':{key:self.serializer.serialize(value) for key,value in row.items()}}
            for row in (user if user is not None else self.user, writer if writer is not None else self.writer)]}

    def controls(self, stub, second=None):
        stub.add_response('transact_get_items', self.response())
        stub.add_response('transact_get_items', second or self.response())

    def run_register(self):
        return self.registrar.register(owner=self.owner, auth_epoch=2)

    def test_complete_audit_cas_binds_account_writer_and_revision_without_model_authority(self):
        with Stubber(self.client) as stub:
            self.controls(stub)
            stub.add_response('transact_write_items', {}, {'ClientRequestToken':ANY,'TransactItems':ANY})
            result = self.run_register()
            stub.assert_no_pending_responses()
        self.assertEqual(result, {'coverage_version':1,'record_count':2,'immediately_previous_verified':False})
        self.assertEqual(len(self.requests), 1)
        update, writer, revision = self.requests[0]['TransactItems']
        self.assertEqual(update['Update']['UpdateExpression'], 'SET history_coverage = :coverage')
        self.assertIn('attribute_not_exists(history_coverage)', update['Update']['ConditionExpression'])
        self.assertIn('attribute_not_exists(deletion_lock_request)', update['Update']['ConditionExpression'])
        coverage = self.decoder.deserialize(update['Update']['ExpressionAttributeValues'][':coverage'])
        self.assertEqual((coverage['coverage_revision'],coverage['generation']), (3,self.writer['generation']))
        self.assertEqual(coverage['writer_manifest_sha256'], self.writer['manifest_sha256'])
        self.store.revisions = [3,3]
        audit = audit_snapshot(self.store, self.owner)
        self.assertEqual((coverage['baseline_roster_sha256'],coverage['proof_sha256']), (audit.roster_sha256,audit.proof_sha256))
        self.assertNotIn(self.owner, repr(audit))
        self.assertNotIn(self.owner, json.dumps(result))
        self.assertIn('valid_until > :now', writer['ConditionCheck']['ConditionExpression'])
        self.assertEqual(revision['ConditionCheck']['ExpressionAttributeValues'], {':revision':{'N':'3'}})
        for action in self.requests[0]['TransactItems']:
            self.assertEqual(next(iter(action.values()))['ReturnValuesOnConditionCheckFailure'], 'NONE')

    def test_replacement_requires_exact_old_coverage_not_unconditional_overwrite(self):
        old = {'version':4,'state':'ACTIVE','proof_sha256':'b'*64}
        self.user['history_coverage'] = old
        with Stubber(self.client) as stub:
            self.controls(stub)
            stub.add_response('transact_write_items', {})
            self.assertEqual(self.run_register()['coverage_version'],5)
        update = self.requests[0]['TransactItems'][0]['Update']
        self.assertIn('history_coverage = :old', update['ConditionExpression'])
        self.assertEqual(self.decoder.deserialize(update['ExpressionAttributeValues'][':old']),old)

    def test_no_coverage_without_independently_enforced_writer_record_or_active_account(self):
        for kind in ['writer_state','expired','contract','table','manifest','epoch','suspended','locked']:
            with self.subTest(kind=kind):
                self.setUp()
                if kind=='writer_state':self.writer['state']='UNVERIFIED'
                elif kind=='expired':self.writer['valid_until']=1000
                elif kind=='contract':self.writer['writer_contract']='old'
                elif kind=='table':self.writer['cooking_table_arn']='foreign'
                elif kind=='manifest':self.writer['manifest_sha256']='invalid'
                elif kind=='epoch':self.user['epoch']=3
                elif kind=='suspended':self.user['suspended']=True
                else:self.user['deletion_lock_request']=str(uuid4())
                with Stubber(self.client) as stub:
                    stub.add_response('transact_get_items',self.response())
                    with self.assertRaises(HistoryCoverageUnavailable):self.run_register()
                    stub.assert_no_pending_responses()
                self.assertEqual(self.requests,[])
                self.assertEqual(self.store.revisions,[3,3])

    def test_dangling_missing_or_changed_revision_does_not_issue_coverage(self):
        for mode in ['missing','dangling','revision','untracked','scan_limit']:
            with self.subTest(mode=mode):
                self.setUp()
                if mode=='missing':self.store.listed.pop()
                elif mode=='dangling':self.store.listed.append({'job_id':str(uuid4()),'created_at':'2026-01-01T00:00:00Z'})
                elif mode=='revision':self.store.revisions=[3,4]
                elif mode=='untracked':self.store.revisions=[None,None]
                else:self.store.table.pages[0]['ScannedCount']=10001
                with Stubber(self.client) as stub:
                    stub.add_response('transact_get_items',self.response())
                    with self.assertRaises(HistoryCoverageUnavailable):self.run_register()
                    stub.assert_no_pending_responses()
                self.assertEqual(self.requests,[])

    def test_writer_or_account_change_after_scan_blocks_registration(self):
        for target in ['writer','user']:
            with self.subTest(target=target):
                self.setUp()
                after = copy.deepcopy(self.writer if target=='writer' else self.user)
                after['version' if target=='writer' else 'epoch'] = 8
                with Stubber(self.client) as stub:
                    self.controls(stub,self.response(**{target:after}))
                    with self.assertRaises(HistoryCoverageUnavailable):self.run_register()
                    stub.assert_no_pending_responses()
                self.assertEqual(self.requests,[])

    def test_conditional_race_or_uncertain_transaction_does_not_retry_or_report_success(self):
        for code in ['TransactionCanceledException','InternalServerError']:
            with self.subTest(code=code):
                self.setUp()
                with Stubber(self.client) as stub:
                    self.controls(stub)
                    stub.add_client_error('transact_write_items',code,'synthetic race',http_status_code=400)
                    with self.assertRaisesRegex(HistoryCoverageUnavailable,'^history coverage registration unavailable$'):
                        self.run_register()
                    stub.assert_no_pending_responses()
                self.assertEqual(len(self.requests),1)

    def test_expiry_after_audit_never_starts_transaction(self):
        self.registrar.clock = iter([1000,1000,1100]).__next__
        with Stubber(self.client) as stub:
            self.controls(stub)
            with self.assertRaises(HistoryCoverageUnavailable):self.run_register()
            stub.assert_no_pending_responses()
        self.assertEqual(self.requests,[])

    def test_registered_coverage_is_consumed_by_full_prior_history_reader(self):
        with Stubber(self.client) as stub:
            self.controls(stub)
            stub.add_response('transact_write_items', {})
            self.run_register()
        coverage = self.decoder.deserialize(self.requests[0]['TransactItems'][0]['Update']['ExpressionAttributeValues'][':coverage'])
        user = {**self.user, 'history_coverage':coverage}
        encode = self.registrar._encode
        controls = {'Responses':[{'Item':encode(row)} for row in [user,self.writer,
            {'pk':'SCOPE#USER#'+self.owner+'#JOBS','sk':'HISTORY#REVISION','revision':3}]]}
        jobs = {}
        listing = []
        for index, job_id in enumerate(self.ids):
            created = self.store.rows[job_id]['created_at']
            listing.append({'pk':'SCOPE#USER#'+self.owner+'#JOBS','sk':'CREATED#'+created+'#'+job_id,
                            'job_id':job_id,'created_at':created})
            day = 3-index
            jobs[job_id] = {**self.store.rows[job_id], 'pk':'SCOPE#USER#'+self.owner+'#JOB#'+job_id,'sk':'META',
                'menu':'JEYUK','provenance':'SYNTHETIC','equipment_profile':'jeyuk-small',
                'status':'RUNNING' if index==0 else 'FAILED','actual_placed_at':f'2026-01-0{day}T00:00:00Z',
                'actual_lifted_at':f'2026-01-0{day}T00:05:00Z',
                'source_binding':{'source_id':'synthetic-rest','device_id':'mockprofile-110-a-001','batch_id':'jeyuk-'+job_id}}
        authority = DynamoPriorHistoryAuthority(auth_table=self.registrar.auth,cooking_table=self.registrar.cooking,
            account_id='123456789012',client=self.client,clock=lambda:1000)
        with Stubber(self.client) as stub:
            stub.add_response('transact_get_items',controls)
            stub.add_response('query',{'Items':[encode(row) for row in listing]})
            stub.add_response('transact_get_items',{'Responses':[{'Item':encode(jobs[key])} for key in sorted(jobs)]})
            stub.add_response('transact_get_items',controls)
            witness = authority.observe(owner=self.owner,auth_epoch=2,current_job_id=self.ids[0],
                prior_job_id=self.ids[1],job_versions=(1,1))
            stub.assert_no_pending_responses()
        self.assertEqual(witness.coverage_proof_sha256,coverage['proof_sha256'])
        self.assertEqual(witness.roster_sha256,coverage['baseline_roster_sha256'])
        self.assertEqual((witness.coverage_version,witness.history_revision),(1,3))
