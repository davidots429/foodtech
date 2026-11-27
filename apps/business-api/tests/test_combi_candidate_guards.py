"""Adopted intent cannot bypass fresh preparation or early transition checks."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
from unittest.mock import patch
import httpx
from test_combi_candidate_meal_plan import plan_input, NOW
from foodtech_business_api import cooking, combi_meal
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.combi_candidate_meal import CandidateMealRequest, candidate_meal_records
from foodtech_business_api.main import app
from foodtech_contracts.combi_transition import TransitionSample, assess_transition
from foodtech_contracts.combi_transition_live import observe

CLOCK=datetime(2026,11,28,3,tzinfo=timezone.utc)


def samples(count, humidity=True):
    return [TransitionSample(i,Decimal(220),Decimal(10),True,False,Decimal(10) if humidity else None)
            for i in range(1,count+1)]


class TransitionTargetTests(unittest.TestCase):
    def test_target_requires_current_conditions_and_keeps_unmeasured_180_second_floor(self):
        for target in (30,120,300):
            result=assess_transition(samples(target),humidity_supported=True,vent_supported=False,minimum_seconds=target)
            self.assertEqual(result['ready_at_second'],target)
            self.assertEqual(result['status'],'DRY_HEAT_READY')
        for target in (30,120):
            self.assertFalse(assess_transition(samples(179,False),humidity_supported=False,vent_supported=False,
                minimum_seconds=target)['mackerel_load_allowed'])
            self.assertEqual(assess_transition(samples(180,False),humidity_supported=False,vent_supported=False,
                minimum_seconds=target)['ready_at_second'],180)
        changed=samples(300);changed[-1]=TransitionSample(300,Decimal(220),Decimal(10),False,False,Decimal(10))
        self.assertEqual(assess_transition(changed,humidity_supported=True,vent_supported=False,
            minimum_seconds=300)['status'],'MODE_TRANSITION_TIMEOUT')
        for target in (True,29,301,'120'):
            with self.assertRaises(ValueError):assess_transition(samples(30),humidity_supported=True,vent_supported=False,minimum_seconds=target)

    def test_live_display_waits_for_the_target_without_granting_loading_permission(self):
        state=None
        for second in range(1,121):
            state=observe(state,{'phase':'TRANSITION','phaseSecond':second,'schemaVersion':'synthetic-combi-v2',
                'humidityValueSource':'MEASURED','ventSupported':False,'ventEvent':None,'chamberCelsius':'220',
                'climateSetpointPercent':'10','doorState':'CLOSED','criticalAlarm':False,'measuredHumidityPercent':'10'},minimum_seconds=120)
            self.assertEqual(state['ready_conditions_current'],second==120)
        self.assertEqual(state['ready_observed_at_second'],120)
        self.assertNotIn('mackerel_load_allowed',state)


class CandidateWorkGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=MemoryCookingStore()
        async def session(_request,_surface):return Session('customer','candidate-customer',None,'csrf',9999999999,0)
        self.patches=[patch.dict(os.environ,{'FOODTECH_SECURE_COOKIES':'false'}),
            patch.object(cooking,'_session',side_effect=session),patch.object(cooking,'get_cooking_store',return_value=self.store),
            patch.object(combi_meal,'datetime',wraps=datetime),patch.object(combi_meal,'_now',return_value=CLOCK.isoformat())]
        for item in self.patches:item.start()
        combi_meal.datetime.now.return_value=CLOCK
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')
        self.headers={'X-CSRF-Token':'csrf'}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):item.stop()

    def prepare(self,menu):
        payload,candidate,review=plan_input(menu)
        records=candidate_meal_records(CandidateMealRequest(**payload),candidate,review,NOW)
        self.meta,self.rice,self.transition,self.fish=records
        self.pk=f"SCOPE#USER#candidate-customer#COMBI_MEAL#{self.meta['meal_batch_id']}"
        self.store.create_meal(self.pk,records,0)
        self.path='/api/customer/combi-meals/'+self.meta['meal_batch_id']
        return self.store.items[self.pk,'META']

    async def start_rice(self,version):
        return await self.client.post(self.path+'/events',headers=self.headers,json={
            'expected_version':version,'event':'RICE_STARTED','observed_at':'2026-11-28T02:40:00Z'})

    async def prep(self):
        return await self.client.post(self.path+'/rice-preparation',headers=self.headers,json={
            'expected_version':1,'wash_count':3,'soaking_water_celsius':'20',
            'soaking_started_at':'2026-11-28T02:00:00Z','soaking_completed_at':'2026-11-28T02:30:00Z',
            'draining_completed_at':'2026-11-28T02:35:00Z'})

    async def test_rice_requires_own_preparation_audit_and_preserves_planned_versus_actual_water(self):
        self.prepare('RICE');before=copy.deepcopy(self.store.items)
        self.assertEqual((await self.start_rice(1)).status_code,409)
        self.assertEqual(self.store.items,before)
        self.assertEqual((await self.prep()).status_code,201)
        result=await self.start_rice(2)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['step']['target_added_water_kg'],'2.04')
        self.assertEqual(result.json()['step']['added_water_kg'],'2')
        self.assertEqual(result.json()['step']['safety_status'],'NOT_MEASURED')
        self.assertEqual(result.json()['step']['target_program_seconds'],1740)

    async def test_rice_missing_mismatched_audit_or_changed_programme_rejects_without_writes(self):
        for change in ('missing-audit','changed-audit','changed-intent','changed-water'):
            self.store.items={};self.prepare('RICE');self.assertEqual((await self.prep()).status_code,201)
            step=self.store.items[self.pk,self.rice['sk']]
            if change=='missing-audit':del self.store.items[self.pk,'EVENT#000000000002']
            elif change=='changed-audit':
                event=self.store.items[self.pk,'EVENT#000000000002']
                event['preparation']={**event['preparation'],'wash_count':4}
            elif change=='changed-intent':step['target_program_seconds']=1800
            else:step['added_water_kg']='3'
            before=copy.deepcopy(self.store.items)
            response=await self.start_rice(2)
            self.assertEqual(response.status_code,409,(change,response.text))
            self.assertEqual(self.store.items,before)

    async def test_manual_transition_cannot_ready_before_adopted_target(self):
        meta=self.prepare('MACKEREL');meta.update(status='MODE_TRANSITION',version=5)
        self.store.items[self.pk,self.transition['sk']].update(status='IN_PROGRESS',started_at='2026-11-28T02:40:00Z')
        def body(count,version):
            return {'expected_version':version,'humidity_supported':True,'vent_supported':False,
                'samples':[{'second':i,'chamber_celsius':'220','climate_setpoint_percent':'10','door_closed':True,
                    'critical_alarm':False,'humidity_percent':'10'} for i in range(1,count+1)]}
        first=await self.client.post(self.path+'/transition-evidence',headers=self.headers,json=body(30,5))
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['meal']['status'],'MODE_TRANSITION')
        self.assertFalse(first.json()['step']['mackerel_load_allowed'])
        ready=await self.client.post(self.path+'/transition-evidence',headers=self.headers,json=body(180,6))
        self.assertEqual(ready.status_code,200,ready.text)
        self.assertEqual(ready.json()['step']['ready_at_second'],180)
        self.assertEqual(ready.json()['meal']['status'],'MACKEREL_READY')
