import json
import unittest
from dataclasses import replace
from decimal import Decimal

from foodtech_contracts.combi_recommendation import CombiBaseline, SCHEMA_VERSION, RULE_VERSION
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import validate
from foodtech_ai_gateway.recommendation_generation import generate_candidate


def baseline(menu='RICE', large=False):
    return CombiBaseline(menu, 'LARGE_ROLL_IN_RACK_20' if large else 'MEDIUM_FIXED_RACK_10',
        2 if large else 1, 2 if large else 1, Decimal(('8.8' if large else '4.4') if menu == 'RICE' else ('3' if large else '1.5')),
        Decimal('2' if large else '1'), Decimal('30' if large else '0'),
        Decimal('100' if menu == 'RICE' else '220'), 1800 if menu == 'RICE' else 720,
        False, True, True, True, Decimal('4' if large else '2'), Decimal('4.8' if large else '2.4'))


def proposed(menu='RICE', large=False, **changes):
    value = {'temperature_c': 100 if menu == 'RICE' else 220,
        'duration_seconds': 1800 if menu == 'RICE' else 720,
        'climate_setpoint_percent': 100 if menu == 'RICE' else 10, 'fan_level': 4 if menu == 'RICE' else 3,
        'pan_positions': [{'rack': 1, 'shelf': 1}] + ([{'rack': 2, 'shelf': 10}] if large else [])}
    value.update({'effective_water_ratio': 1.2, 'soak_seconds': 1800, 'drain_seconds': 300} if menu == 'RICE'
                 else {'transition_duration_seconds': 180})
    return {**value, **changes}


class CombiProposalTests(unittest.TestCase):
    evidence = Evidence('COMPLETE', 'PASSED', True, False)

    def result(self, menu='RICE', *, changes=None, source=None, evidence=None, large=False):
        return validate(json.dumps(proposed(menu, large, **(changes or {}))), evidence or self.evidence,
                        source or baseline(menu, large))

    def test_both_sizes_and_menus_make_candidates_with_separate_wire_and_guard_versions(self):
        for menu in ('RICE', 'MACKEREL'):
            for large in (False, True):
                with self.subTest(menu=menu, large=large):
                    r = self.result(menu, large=large)
                    self.assertEqual(r['status'], 'NEEDS_REVIEW', r)
                    self.assertEqual((r['schema_version'], r['guard_version']), (SCHEMA_VERSION, RULE_VERSION))
                    self.assertFalse(r['fallback_used'])
                    self.assertEqual(r['data']['pan_positions'], proposed(menu, large)['pan_positions'])

    def test_observed_temperature_bands_do_not_expand_fixed_programme_settings(self):
        for menu, change in [('RICE', {'temperature_c': 102}), ('MACKEREL', {'temperature_c': 225}),
                ('RICE', {'fan_level': 3}), ('MACKEREL', {'climate_setpoint_percent': 15}),
                ('RICE', {'soak_seconds': 1799}), ('RICE', {'drain_seconds': 299})]:
            with self.subTest(menu=menu, change=change):
                r = self.result(menu, changes=change)
                self.assertEqual((r['status'], r['data']), ('REJECTED', {}))

    def test_existing_process_time_boundaries_and_water_ratio_endpoints(self):
        for menu, low, high in [('RICE', 1680, 1920), ('MACKEREL', 660, 780)]:
            for seconds in (low - 1, low, high, high + 1):
                with self.subTest(menu=menu, seconds=seconds):
                    self.assertEqual(self.result(menu, changes={'duration_seconds': seconds})['status'],
                                     'NEEDS_REVIEW' if low <= seconds <= high else 'REJECTED')
        for ratio in (1.14, 1.15, 1.25, 1.26):
            self.assertEqual(self.result(changes={'effective_water_ratio': ratio})['status'],
                             'NEEDS_REVIEW' if 1.15 <= ratio <= 1.25 else 'REJECTED')

    def test_negative_added_water_and_candidate_rated_load_are_rejected(self):
        source = replace(baseline(), drained_rice_kg=Decimal('4.6'), ingredient_kg=Decimal('5.5'))
        self.assertIn('NEGATIVE_ADDED_WATER', self.result(source=source)['reasons'])
        source = replace(baseline(), pan_count=10, ingredient_kg=Decimal('40'), pan_tare_kg=Decimal('16'),
                         dry_rice_kg=Decimal('20'), drained_rice_kg=Decimal('22'))
        r = self.result(source=source, changes={'effective_water_ratio': 1.25,
                         'pan_positions': [{'rack': 1, 'shelf': i} for i in range(1, 11)]})
        self.assertEqual((r['status'], r['data']), ('REJECTED', {}))
        self.assertIn('RATED_LOAD_EXCEEDED', r['reasons'])

    def test_unknown_invalid_or_interrupted_sources_cannot_be_normalized_to_good_evidence(self):
        for change in ({'humidity_available': False}, {'humidity_available': None}, {'safety_basis_valid': False},
                       {'process_interrupted': None}, {'process_interrupted': True}, {'rack_count': True},
                       {'ingredient_kg': Decimal('NaN')}, {'pan_count': 11}, {'dry_rice_kg': None}):
            with self.subTest(change=change):
                self.assertEqual((self.result(source=replace(baseline(), **change))['status']), 'INDETERMINATE')
        for evidence in (replace(self.evidence, severe_data_gap=True), replace(self.evidence, critical_data_valid=None),
                         replace(self.evidence, safety_status='NOT_MEASURED')):
            self.assertEqual(self.result(evidence=evidence)['status'], 'INDETERMINATE')

    def test_failure_never_allows_shorter_time_or_lower_target(self):
        failed = replace(self.evidence, safety_status='FAILED')
        source = replace(baseline(), previous_duration_seconds=1900, previous_temperature_c=Decimal('101'))
        r = self.result(source=source, evidence=failed)
        self.assertEqual(r['status'], 'REJECTED')
        self.assertIn('SHORTENING_AFTER_SAFETY_FAILURE', r['reasons'])
        self.assertIn('LOWERING_TEMPERATURE_AFTER_SAFETY_FAILURE', r['reasons'])
        self.assertEqual(self.result(source=replace(source, previous_duration_seconds=None), evidence=failed)['status'],
                         'INDETERMINATE')

    def test_transition_requires_readiness_and_preserves_unsupported_humidity_basis(self):
        self.assertEqual(self.result('MACKEREL', source=replace(baseline('MACKEREL'), transition_ready=False))['status'],
                         'INDETERMINATE')
        source = replace(baseline('MACKEREL'), humidity_available=False)
        for seconds, status in [(179, 'REJECTED'), (180, 'NEEDS_REVIEW'), (300, 'NEEDS_REVIEW'), (301, 'REJECTED')]:
            self.assertEqual(self.result('MACKEREL', source=source, changes={'transition_duration_seconds': seconds})['status'],status)
        self.assertEqual(self.result('MACKEREL', changes={'transition_duration_seconds':30})['status'], 'NEEDS_REVIEW')

    def test_layout_requires_exact_pan_count_and_unique_positions_inside_existing_racks(self):
        for positions in ([{'rack':2,'shelf':1}], [{'rack':1,'shelf':11}],
                          [{'rack':1,'shelf':1}]*2, [{'rack':True,'shelf':1}], [{'rack':1,'shelf':1,'note':'secret'}]):
            with self.subTest(positions=positions):
                r = self.result(changes={'pan_positions':positions})
                self.assertNotEqual(r['status'],'NEEDS_REVIEW'); self.assertEqual(r['data'],{})
                self.assertNotIn('secret',str(r))

    def test_strict_numbers_duplicates_and_unknown_model_fields_fail_without_echo(self):
        for change in ({'fan_level':True},{'duration_seconds':1800.0},{'temperature_c':'100'},
                       {'secret':'private raw value'},{'pan_positions':'controller expression'}):
            r = self.result(changes=change)
            self.assertEqual((r['status'],r['data']),('FAILED',{})); self.assertNotIn('private raw value',str(r))
        raw=json.dumps(proposed()).replace('"temperature_c": 100','"temperature_c": 100, "temperature_c": 101')
        self.assertEqual(validate(raw,self.evidence,baseline())['status'],'FAILED')


class CombiGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_is_nested_and_only_malformed_output_retries(self):
        calls=[]
        async def invoke(messages,schema):
            calls.append(schema)
            return '{}' if len(calls)==1 else json.dumps(proposed())
        r=await generate_candidate(invoke,evidence=Evidence('COMPLETE','PASSED',True,False),baseline=baseline(),context={'synthetic':True})
        self.assertEqual((r['status'],r['attempts']),('NEEDS_REVIEW',2))
        schema=calls[0];self.assertFalse(schema['additionalProperties'])
        self.assertFalse(schema['properties']['pan_positions']['items']['additionalProperties'])
        self.assertEqual(schema['properties']['temperature_c']['enum'],[100])
        self.assertEqual(schema['properties']['pan_positions']['maxItems'],1)
        calls.clear()
        async def rejected(messages,schema):
            calls.append(schema);return json.dumps(proposed(temperature_c=102))
        r=await generate_candidate(rejected,evidence=Evidence('COMPLETE','PASSED',True,False),baseline=baseline(),context={})
        self.assertEqual((r['status'],r['attempts'],len(calls)),('REJECTED',1,1))

    async def test_missing_and_malformed_trusted_evidence_never_call_provider(self):
        calls=[]
        async def invoke(*args):calls.append(args);raise AssertionError('provider must not run')
        for evidence,source in [(Evidence('COMPLETE','PASSED',True,False),replace(baseline(),safety_basis_valid=None)),
                                (Evidence([], 'PASSED',True,False),baseline())]:
            r=await generate_candidate(invoke,evidence=evidence,baseline=source,context={})
            self.assertEqual(r['attempts'],0)
        self.assertEqual(calls,[])

    async def test_unsupported_humidity_schema_and_layout_are_request_scoped(self):
        calls=[]
        async def invoke(messages,schema):
            calls.append(schema);return json.dumps(proposed('MACKEREL',True))
        await generate_candidate(invoke,evidence=Evidence('COMPLETE','PASSED',True,False),
            baseline=replace(baseline('MACKEREL',True),humidity_available=False),context={})
        self.assertEqual(calls[0]['properties']['transition_duration_seconds']['minimum'],180)
        self.assertEqual(calls[0]['properties']['pan_positions']['minItems'],2)
        self.assertEqual(calls[0]['properties']['pan_positions']['items']['properties']['rack']['maximum'],2)
        async def other(messages,schema):
            calls.append(schema);return json.dumps(proposed('MACKEREL'))
        await generate_candidate(other,evidence=Evidence('COMPLETE','PASSED',True,False),baseline=baseline('MACKEREL'),context={})
        self.assertEqual(calls[1]['properties']['transition_duration_seconds']['minimum'],30)
        self.assertEqual(calls[1]['properties']['pan_positions']['maxItems'],1)
