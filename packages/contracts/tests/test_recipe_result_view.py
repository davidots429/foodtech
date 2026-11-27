"""Stored proposal projection is bounded, owner-bound and never a raw result reader."""
import copy
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from foodtech_contracts.recipe_result_view import recipe_execution_result_payload, recipe_proposal_digest, validate_recipe_result_payload
from foodtech_contracts.recommendation_output import SCHEMA_VERSION
from foodtech_contracts.recommendation_guard import RULE_VERSION


class RecipeResultViewTests(unittest.TestCase):
    def test_review_digest_is_order_independent_and_changes_with_exact_proposal(self):
        proposal = self.view()['proposal']
        reordered = dict(reversed(list(proposal.items())))
        reordered['data'] = dict(reversed(list(proposal['data'].items())))
        self.assertEqual(recipe_proposal_digest(proposal), recipe_proposal_digest(reordered))
        changed = copy.deepcopy(proposal)
        changed['data']['temperature_c'] = '201'
        self.assertNotEqual(recipe_proposal_digest(proposal), recipe_proposal_digest(changed))
        with self.assertRaises(ValueError):
            recipe_proposal_digest({**proposal, 'credential': 'private-marker'})

    def setUp(self):
        self.execution_id,self.job_id=str(uuid4()),str(uuid4())
        self.snapshot=SimpleNamespace(execution=SimpleNamespace(execution_id=self.execution_id,tenant_id='owner',
            feature='RECIPE_CORRECTION',job_id=self.job_id,job_version=3),state='SUCCEEDED',version=2)
        self.proposal={'status':'NEEDS_REVIEW','error_code':None,'schema_version':SCHEMA_VERSION,
            'guard_version':RULE_VERSION,'fallback_used':False,'attempts':1,
            'data':{'temperature_c':'200','rpm':'60','duration_seconds':300,'load_kg':'7',
                    'vegetable_interval_seconds':60,'sauce_interval_seconds':120}}
        self.result={'state':'SUCCEEDED','response_text':json.dumps(self.proposal),
            'credential':'private-key-marker','provider_response':'private-body-marker'}

    def view(self, **kwargs):
        return recipe_execution_result_payload(self.snapshot,self.result,execution_id=self.execution_id,
                                                tenant_id='owner',job_id=self.job_id,**kwargs)

    def test_success_contains_only_typed_proposal_not_raw_response_or_credentials(self):
        view=self.view()
        self.assertEqual(view['projection_status'],'AVAILABLE')
        self.assertEqual(view['proposal']['menu'],'JEYUK')
        self.assertEqual(view['proposal']['status'],'NEEDS_REVIEW')
        self.assertIs(view['execution_available'],False)
        self.assertNotIn('response_text',view)
        self.assertNotIn('private-',json.dumps(view))
        self.assertEqual(set(view['proposal']),{'status','schema_version','guard_version','menu','data'})

    def test_tonkatsu_projection_does_not_invent_jeyuk_fields(self):
        self.proposal['data']={'temperature_c':'170.00','duration_seconds':300}
        self.result['response_text']=json.dumps(self.proposal)
        view=self.view()
        self.assertEqual(view['proposal']['menu'],'TONKATSU')
        self.assertEqual(view['proposal']['data'],self.proposal['data'])

    def test_unfinished_and_failed_records_never_expose_provider_messages(self):
        for state in ('PENDING','RESERVED'):
            self.snapshot.state=state;self.result=None
            self.assertEqual(self.view()['projection_status'],'NOT_READY')
        self.snapshot.state='FAILED';self.result={'state':'FAILED','response_text':'private-key-marker','error_code':'private-provider-marker'}
        view=self.view()
        self.assertEqual(view['error_code'],'RECIPE_EXECUTION_FAILED')
        self.assertIsNone(view['proposal'])
        self.assertNotIn('private-',json.dumps(view))

    def test_corrupt_unsupported_duplicate_and_oversized_raw_output_is_hidden(self):
        variants=[{**self.proposal,'credential':'private-key-marker'},
            {**self.proposal,'schema_version':'future'}, {**self.proposal,'guard_version':'future'},
            {**self.proposal,'attempts':True}, {**self.proposal,'fallback_used':True},
            {**self.proposal,'error_code':'private-key-marker'}]
        raw=[json.dumps(v) for v in variants]+['{"status":1,"status":2}', 'x'*16385, 'NaN','[]','null','"private-key-marker"']
        for value in raw:
            self.result['response_text']=value
            view=self.view()
            self.assertEqual(view['projection_status'],'UNAVAILABLE')
            self.assertIsNone(view['proposal'])
            self.assertNotIn('private-',json.dumps(view))

    def test_invalid_numeric_fields_never_reach_the_view(self):
        for field,value in [('rpm','NaN'),('rpm','Infinity'),('load_kg','1E+9999'),('temperature_c','private-key-marker'),
                ('temperature_c',' 200 '),('temperature_c','2_00'),('temperature_c','２００'),
                ('duration_seconds',True),('duration_seconds',300.0),('duration_seconds',-1),('duration_seconds',2**31),
                ('rpm',None),('load_kg',7.0)]:
            proposal=copy.deepcopy(self.proposal);proposal['data'][field]=value
            self.result['response_text']=json.dumps(proposal)
            self.assertEqual(self.view()['projection_status'],'UNAVAILABLE')

    def test_foreign_owner_other_job_feature_or_conflicting_result_is_rejected(self):
        for field,value in [('tenant_id','foreign'),('job_id',str(uuid4())),('feature','GENERAL_GUIDE')]:
            old=getattr(self.snapshot.execution,field);setattr(self.snapshot.execution,field,value)
            with self.assertRaises(ValueError):self.view()
            setattr(self.snapshot.execution,field,old)
        self.result['state']='FAILED'
        with self.assertRaises(ValueError):self.view()

    def test_consumer_revalidates_no_extra_fields_or_unearned_approval(self):
        valid=self.view()
        for change in [{'credential':'private-key-marker'}, {'execution_available':True},
                {'proposal':{**valid['proposal'],'status':'APPROVED'}},
                {'proposal':{**valid['proposal'],'menu':'TONKATSU'}},
                {'projection_status':'NOT_READY'}]:
            with self.assertRaises(ValueError):
                validate_recipe_result_payload({**valid,**change},execution_id=self.execution_id,job_id=self.job_id)
