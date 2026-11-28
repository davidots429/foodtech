"""Candidate programmes and fresh measurements must reach fresh meal records."""
import copy
from decimal import Decimal
import unittest
from uuid import uuid4
from pydantic import ValidationError
from fastapi import HTTPException
from test_combi_review_store import record, MEAL, JOB
from foodtech_business_api.combi_recipe_candidate import build_combi_candidate
from foodtech_business_api.combi_candidate_meal import CandidateMealRequest, candidate_meal_records
from foodtech_contracts.recipe_result_view import recipe_proposal_digest
from foodtech_contracts.combi_cooking import rice_water

NOW='2026-11-28T02:30:00Z'


def plan_input(menu='RICE'):
    review=record();review['menu']=menu;review['proposal']['menu']=menu
    if menu=='RICE':
        review['proposal']['data'].update(duration_seconds=1740,effective_water_ratio='1.22')
    else:
        review['proposal']['data']={'temperature_c':'220','climate_setpoint_percent':'10','fan_level':3,
            'duration_seconds':750,'pan_positions':[{'rack':1,'shelf':2}], 'transition_duration_seconds':180}
    review['proposal_digest']=recipe_proposal_digest(review['proposal'])
    meta={'meal_batch_id':MEAL,'version':3,'provenance':'SYNTHETIC',
        ('rice_cooking_job_id' if menu=='RICE' else 'mackerel_cooking_job_id'):JOB,
        'equipment_size':'MEDIUM_FIXED_RACK_10'}
    candidate=build_combi_candidate(review,meta,'2026-11-28T02:00:00Z')
    payload={'request_id':str(uuid4()),'meal_id':MEAL,'menu':menu,'expected_meal_version':3,
        'proposal_digest':review['proposal_digest'],'confirmed_pan_positions':review['proposal']['data']['pan_positions'],
        'acknowledged':True,'new_meal':{'equipment_size':'MEDIUM_FIXED_RACK_10','rack_count':1,'pan_count':1,
            'dry_rice_kg':'2','drained_rice_kg':'2.4','added_water_kg':'2','pan_tare_kg':'1','rack_tare_kg':'0'}}
    return payload,candidate,review


class CombiCandidateMealPlanTests(unittest.TestCase):
    def setup_candidate(self,menu='RICE'):
        return plan_input(menu)

    def test_rice_programme_water_target_and_layout_are_applied_without_inheriting_facts(self):
        body,candidate,review=self.setup_candidate()
        original=copy.deepcopy(candidate)
        records=candidate_meal_records(CandidateMealRequest(**body),candidate,review,NOW)
        meta,rice,transition,fish=records
        self.assertEqual(meta['meal_batch_id'],body['request_id']);self.assertNotEqual(meta['meal_batch_id'],MEAL)
        self.assertEqual(meta['rice_recipe_version'],candidate['candidate_id']);self.assertNotIn('mackerel_recipe_version',meta)
        self.assertEqual(meta['version'],1);self.assertEqual(meta['status'],'PREPARING_RICE')
        self.assertEqual(rice['target_program_seconds'],1740);self.assertEqual(rice['target_effective_water_ratio'],'1.22')
        self.assertEqual(rice['target_added_water_kg'],'2.04');self.assertEqual(rice['effective_water_ratio'],'1.2')
        self.assertEqual(rice['planned_pan_positions'],body['confirmed_pan_positions'])
        self.assertEqual(rice['target_soaking_seconds'],1800);self.assertEqual(rice['target_draining_seconds'],300)
        self.assertEqual(rice['status'],'PENDING');self.assertEqual(rice['safety_status'],'NOT_MEASURED')
        self.assertEqual(rice['quality_status'],'PENDING');self.assertEqual(rice['process_status'],'NOT_MEASURED')
        self.assertNotIn('rice_preparation',rice);self.assertNotIn('source',meta);self.assertNotIn('load_snapshot',fish)
        self.assertFalse(transition['mackerel_load_allowed']);self.assertEqual(fish['target_program_seconds'],720)
        self.assertEqual(candidate,original)
        self.assertEqual(meta['recipe_candidate_selection']['candidate'],candidate)
        self.assertEqual(meta['recipe_candidate_selection']['review'],review)

    def test_mackerel_programme_and_transition_target_do_not_reuse_products_or_sensor_support(self):
        body,candidate,review=self.setup_candidate('MACKEREL')
        body['new_meal']['rice_recipe_version']='explicit-new-rice-baseline'
        meta,rice,transition,fish=candidate_meal_records(CandidateMealRequest(**body),candidate,review,NOW)
        self.assertEqual(meta['mackerel_recipe_version'],candidate['candidate_id'])
        self.assertEqual(meta['rice_recipe_version'],'explicit-new-rice-baseline')
        self.assertEqual(fish['target_program_seconds'],750);self.assertEqual(fish['planned_pan_positions'],[{'rack':1,'shelf':2}])
        self.assertEqual(transition['target_duration_seconds'],180);self.assertFalse(transition['mackerel_load_allowed'])
        self.assertEqual(fish['safety_status'],'NOT_MEASURED');self.assertNotIn('products',fish)
        self.assertNotIn('humidity_supported',transition);self.assertEqual(rice['target_program_seconds'],1800)
        self.assertNotIn('target_effective_water_ratio',rice)

    def test_request_identity_and_decimal_normalization_are_stable_without_mutating_measurements(self):
        body,candidate,review=self.setup_candidate()
        first=candidate_meal_records(CandidateMealRequest(**body),candidate,review,NOW)
        changed=copy.deepcopy(body);changed['new_meal']['dry_rice_kg']='2.00';changed['new_meal']['added_water_kg']='2.0'
        second=candidate_meal_records(CandidateMealRequest(**changed),candidate,review,NOW)
        self.assertEqual(first[0]['recipe_candidate_selection']['request_hash'],second[0]['recipe_candidate_selection']['request_hash'])
        self.assertEqual([(r['sk'],r.get('job_id')) for r in first],[(r['sk'],r.get('job_id')) for r in second])
        changed['new_meal']['added_water_kg']='2.01'
        third=candidate_meal_records(CandidateMealRequest(**changed),candidate,review,NOW)
        self.assertNotEqual(first[0]['recipe_candidate_selection']['request_hash'],third[0]['recipe_candidate_selection']['request_hash'])

    def test_wrong_scope_profile_confirmation_layout_or_static_programme_cannot_be_selected(self):
        body,candidate,review=self.setup_candidate()
        for changes in ({'meal_id':str(uuid4())},{'expected_meal_version':4},{'proposal_digest':'0'*64},
            {'confirmed_pan_positions':[{'rack':1,'shelf':2}]}):
            with self.assertRaises(ValueError):candidate_meal_records(CandidateMealRequest(**{**body,**changes}),candidate,review,NOW)
        for changes in ({'acknowledged':False},{'request_id':MEAL},
            {'confirmed_pan_positions':[{'rack':1,'shelf':1},{'rack':1,'shelf':1}]}):
            with self.assertRaises(ValidationError):CandidateMealRequest(**{**body,**changes})
        for change in ({'rack_count':True},{'rice_recipe_version':'injected-label'}):
            with self.assertRaises(ValidationError):CandidateMealRequest(**{**body,'new_meal':{**body['new_meal'],**change}})
        for field,value in [('temperature_c','101'),('duration_seconds',1600),('fan_level',3),('effective_water_ratio','1.26')]:
            changed_review=copy.deepcopy(review);changed_review['proposal']['data'][field]=value
            changed_review['proposal_digest']=recipe_proposal_digest(changed_review['proposal'])
            changed_candidate=build_combi_candidate(changed_review,{'meal_batch_id':MEAL,'version':3,'provenance':'SYNTHETIC',
                'rice_cooking_job_id':JOB,'equipment_size':'MEDIUM_FIXED_RACK_10'},'2026-11-28T02:00:00Z')
            request=CandidateMealRequest(**{**body,'proposal_digest':changed_review['proposal_digest']})
            with self.assertRaises(ValueError):candidate_meal_records(request,changed_candidate,changed_review,NOW)

    def test_fresh_load_rules_and_unachievable_water_target_are_not_bypassed(self):
        body,candidate,review=self.setup_candidate()
        for change in ({'drained_rice_kg':'5'},{'pan_tare_kg':'99999'}):
            with self.assertRaises((ValueError,HTTPException)):
                candidate_meal_records(CandidateMealRequest(**{**body,'new_meal':{**body['new_meal'],**change}}),candidate,review,NOW)
        for now in ('2026-11-28T01:30:00Z','2026-11-28T03:00:00'):
            with self.assertRaises(ValueError):candidate_meal_records(CandidateMealRequest(**body),candidate,review,now)
        self.assertEqual(rice_water(Decimal('2'),Decimal('2.4'),Decimal('2')).target_added_water_kg,Decimal('2.00'))
        for ratio in (Decimal('1.14'),Decimal('1.26'),Decimal('NaN'),1.2):
            with self.assertRaises(ValueError):rice_water(Decimal('2'),Decimal('2.4'),Decimal('2'),target_ratio=ratio)
