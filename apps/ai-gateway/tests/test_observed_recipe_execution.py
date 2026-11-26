"""Server-derived recommendation inputs; real parsers/receipts, mocked AWS."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_ai_gateway.policy import Caller, Execution, Feature
from foodtech_ai_gateway.prior_history_conditions import DynamoPriorHistoryConditions
from foodtech_ai_gateway.recipe_read_authorization import DynamoRecipeReadAuthorization
from foodtech_ai_gateway.recipe_source_guard import BoundRecipeSourceGuard, DynamoRecipeSourceGuard
from foodtech_ai_gateway.service import GatewayService, ExecutionOutcome
from foodtech_contracts.prior_history_witness import PriorHistoryWitness
from foodtech_contracts.source_read_authorization import SourceReadAuthorization
from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker
from foodtech_contracts.recommendation_output import validate
from test_recipe_source_guard import FakeResource


class ObservedRecipeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.ids = 'private-owner-marker', (str(uuid4()), str(uuid4()))
        self.execution = Execution(str(uuid4()), self.owner, Feature.RECIPE_CORRECTION, 'model', None,
            'SYNTHETIC', Decimal(0), job_id=self.ids[0], job_version=7, prior_job_id=self.ids[1],
            prior_job_version=3, jeyuk_source_revision=64, prior_source_revision=64)
        self.resource = FakeResource()
        self.sources = ('JEYUK#batch-current', 'JEYUK#batch-prior')
        self.jobs = tuple(f'SCOPE#USER#{self.owner}#JOB#{job}' for job in self.ids)
        for index, job_id in enumerate(self.ids):
            placed = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=index * 120)
            iso = lambda seconds: (placed + timedelta(seconds=seconds)).isoformat()
            batch = self.sources[index][6:]
            job = {'owner_subject': self.owner, 'job_id': job_id, 'version': (7, 3)[index], 'menu': 'JEYUK',
                'provenance': 'SYNTHETIC', 'jeyuk_size_profile': 'SMALL', 'equipment_profile': 'jeyuk-small',
                'actual_placed_at': iso(0), 'actual_lifted_at': iso(64), 'actual_cooking_seconds': 64,
                'actual_load_kg': '1.8', 'safety_status': 'PASSED', 'notes': 'private-note-marker',
                'source_binding': {'source_id': 'synthetic-rest', 'device_id': 'mockprofile-110-a-001', 'batch_id': batch},
                'jeyuk_addition_group_id': 'group', 'jeyuk_additions_simultaneous': True,
                'jeyuk_feeds': {name: {'ingredient': name, 'provenance': 'SYNTHETIC', 'started_at': iso(start),
                    'completed_at': iso(end), 'addition_group_id': 'group', 'mass_kg': mass,
                    'initial_temperature_c': '4', 'temperature_observed_at': iso(-3)} for name, start, end, mass in
                    [('MEAT', -2, 0, '1.0'), ('VEGETABLE', 1, 1.5, '0.5'), ('SAUCE', 2, 2.5, '0.3')]}}
            cooking = self.resource.tables['foodtech-cooking-jobs-dev'].items
            cooking[(self.jobs[index], 'META')] = job
            cooking[(self.sources[index], 'BINDING')] = {'job_pk': self.jobs[index], 'job_id': job_id}
            data = self.resource.tables['foodtech-data-flow-dev'].items
            data[(self.sources[index], 'SOURCE#REVISION')] = {'revision': 64}
            for second in range(1, 65):
                sample = {'fixtureId': batch, 'provenance': 'SYNTHETIC', 'profileId': 'mockprofile-110',
                    'instanceId': 'mockprofile-110-a-001', 'profile': 'SMALL', 'actualLoadKg': 1.8,
                    'second': second, 'sampleIntervalSeconds': 1, 'surfaceCelsius': 190,
                    'heatingSetpointCelsius': 200, 'actualRpm': 30, 'targetRpm': 30, 'motorLoadPercent': 40,
                    'mixtureCelsius': 75, 'calibrationStatus': 'OK', 'safetySensorPosition': 'LOWEST_MIXTURE_POINT',
                    'lastIngredientAdditionCompleted': second == 3, 'observedAt': iso(second)}
                sk = f'RAW#{second:06d}'
                data[(self.sources[index], sk)] = {'pk': self.sources[index], 'sk': sk,
                    'provenance': 'SYNTHETIC', 'raw': json.dumps(sample)}
            self.receipt(index)
        self.conditions = MagicMock()
        grant = SourceReadAuthorization('foodtech-auth-sessions-dev', 'foodtech-source-access-dev', self.owner,
            self.execution.execution_id, 0, self.sources, (str(uuid4()), str(uuid4())), (1, 1), (2000, 2000))
        auth = DynamoRecipeReadAuthorization(grant, client=self.conditions, clock=lambda: 1000)
        witness = PriorHistoryWitness(self.owner, *self.ids, (7, 3), 0, 10, 1, 1, str(uuid4()),
            'a' * 64, 'b' * 64, 10, 'a' * 64, 'c' * 64)
        history = DynamoPriorHistoryConditions(witness, auth_table='foodtech-auth-sessions-dev',
            cooking_table='foodtech-cooking-jobs-dev', account_id='123456789012', client=self.conditions, clock=lambda: 1000)
        self.guard = BoundRecipeSourceGuard(DynamoRecipeSourceGuard('foodtech-cooking-jobs-dev',
            'foodtech-data-flow-dev', resource=self.resource), self.execution, self.jobs, self.sources, auth, history)
        self.registry = MagicMock()
        self.registry.execution.return_value = self.execution
        self.service = GatewayService(self.registry, recipe_source_guard=self.guard)
        self.service.execute_local_recipe = AsyncMock(return_value=ExecutionOutcome(self.execution.execution_id, 'SUCCEEDED'))
        self.model = object()

    def receipt(self, index):
        data = self.resource.tables['foodtech-data-flow-dev'].items
        tracker = JeyukCompletionTracker(0)
        for second in range(1, 65):
            tracker.record(data[(self.sources[index], f'RAW#{second:06d}')]['raw'])
        receipt = tracker.finish(cursor=64, complete=True, explicit=True)
        receipt.update(pk=self.sources[index], sk='COLLECTION#RECEIPT#1')
        data[(self.sources[index], receipt['sk'])] = receipt

    def change_sample(self, index, second, **changes):
        row = self.resource.tables['foodtech-data-flow-dev'].items[(self.sources[index], f'RAW#{second:06d}')]
        value = json.loads(row['raw'])
        value.update(changes)
        row['raw'] = json.dumps(value)

    def run_flow(self, owner=None):
        return asyncio.run(self.service.execute_observed_local_recipe(Caller('business-api', owner or self.owner),
            self.execution.execution_id, ollama=self.model))

    def test_complete_sources_derive_only_prior_baseline_and_redacted_context(self):
        self.assertEqual(self.run_flow().state, 'SUCCEEDED')
        args = self.service.execute_local_recipe.call_args.kwargs
        self.assertEqual(args['baseline'].previous_duration_seconds, 64)
        self.assertEqual(args['baseline'].previous_temperature_c, Decimal(200))
        self.assertEqual((args['evidence'].completeness, args['evidence'].safety_status), ('COMPLETE', 'PASSED'))
        text = json.dumps(args['context'])
        for private in [self.owner, *self.ids, 'private-note-marker', *self.sources, 'batch-current', 'batch-prior']:
            self.assertNotIn(private, text)
        self.assertGreaterEqual(self.conditions.transact_write_items.call_count, 2)

    def test_prior_safety_failure_limits_temperature_even_when_current_passed(self):
        self.resource.tables['foodtech-cooking-jobs-dev'].items[(self.jobs[1], 'META')]['safety_status'] = 'FAILED'
        for second in range(1, 65):
            self.change_sample(1, second, heatingSetpointCelsius=210)
        self.receipt(1)
        self.run_flow()
        args = self.service.execute_local_recipe.call_args.kwargs
        self.assertEqual(args['evidence'].safety_status, 'FAILED')
        result = validate(json.dumps({'temperature_c':200,'rpm':30,'duration_seconds':480,'load_kg':1.8,
            'vegetable_interval_seconds':1,'sauce_interval_seconds':2}), args['evidence'], args['baseline'])
        self.assertIn('LOWERING_TEMPERATURE_AFTER_SAFETY_FAILURE', result['reasons'])

    def test_prior_overload_is_not_lost_when_current_has_none(self):
        self.change_sample(1, 1, motorLoadPercent=101)
        self.receipt(1)
        self.run_flow()
        args = self.service.execute_local_recipe.call_args.kwargs
        self.assertTrue(args['baseline'].previous_jam_or_overload)
        result = validate(json.dumps({'temperature_c':200,'rpm':35,'duration_seconds':480,'load_kg':1.8,
            'vegetable_interval_seconds':1,'sauce_interval_seconds':2}), args['evidence'], args['baseline'])
        self.assertIn('INCREASED_RPM_AFTER_JAM', result['reasons'])

    def test_unresolved_inputs_or_false_stored_safety_pass_do_not_reach_model_boundary(self):
        for mode in ['missing_setting','missing_temperature','calibration','hold','lost_hold','mass','not_measured']:
            with self.subTest(mode=mode):
                self.setUp()
                job = self.resource.tables['foodtech-cooking-jobs-dev'].items[(self.jobs[1], 'META')]
                if mode == 'missing_setting':
                    row = self.resource.tables['foodtech-data-flow-dev'].items[(self.sources[1], 'RAW#000001')]
                    value = json.loads(row['raw'])
                    del value['heatingSetpointCelsius']
                    row['raw'] = json.dumps(value)
                elif mode == 'missing_temperature': del job['jeyuk_feeds']['MEAT']['initial_temperature_c']
                elif mode == 'calibration': self.change_sample(1, 1, calibrationStatus='EXPIRED')
                elif mode == 'hold':
                    for second in range(1, 65): self.change_sample(1, second, mixtureCelsius=74)
                elif mode == 'lost_hold': self.change_sample(1, 64, mixtureCelsius=74)
                elif mode == 'mass': job['jeyuk_feeds']['MEAT']['mass_kg'] = '1.1'
                else: job['safety_status'] = 'NOT_MEASURED'
                self.receipt(1)
                self.assertEqual(self.run_flow().error_code, 'RECOMMENDATION_EVIDENCE_UNVERIFIED')
                self.service.execute_local_recipe.assert_not_called()

    def test_changed_raw_receipt_or_history_and_wrong_owner_do_not_delegate(self):
        for mode in ['raw','history','owner','missing_history','execution']:
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'raw': self.change_sample(0, 1, heatingSetpointCelsius=205)
                elif mode == 'history': self.conditions.transact_write_items.side_effect = ClientError(
                    {'Error': {'Code':'TransactionCanceledException','Message':'synthetic'}}, 'TransactWriteItems')
                elif mode == 'missing_history': self.service.recipe_source_guard = replace(self.guard, history_authorization=None)
                elif mode == 'execution': self.registry.execution.side_effect = [self.execution, replace(self.execution, job_version=8)]
                result = self.run_flow('foreign' if mode == 'owner' else None)
                self.assertEqual(result.state, 'DENIED')
                self.service.execute_local_recipe.assert_not_called()
