"""SDK metadata fixtures, not a protocol Mock or deployment attestation."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import unittest
from uuid import uuid4, uuid5, NAMESPACE_DNS
from unittest.mock import patch

from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
from botocore.session import get_session
from botocore.validate import validate_parameters
from botocore.paginate import Paginator

from foodtech_business_api.combi_prior_history import DynamoCombiPriorHistoryAuthority
from foodtech_business_api.prior_history_authority import PriorHistoryUnavailable
from foodtech_contracts.combi_execution_binding import CombiExecutionBinding
from foodtech_contracts.combi_history_witness import (HISTORY_CONTRACT, COVERAGE_FIELD, COVERAGE_SCOPE,
    CONTROL_KEY, roster_digest, history_integer)

NOW = int(datetime(2026, 1, 6, tzinfo=timezone.utc).timestamp())
SIZE = 'MEDIUM_FIXED_RACK_10'
uid = lambda name: str(uuid5(NAMESPACE_DNS, 'combi-history-test-' + name))


class HistoryClient:
    """Validate real SDK input shapes; return isolated encoded fixture records."""
    def __init__(self, owner):
        self.owner = owner; self.items = {}; self.calls = []; self.control_reads = 0; self.on_second_control = None
        self.page_size = 50; self.bad_cursor = None; self.write_error = None
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()
        self.model = get_session().get_service_model('dynamodb')

    def encode(self, row): return {k: self.serializer.serialize(v) for k, v in row.items()}
    def decode(self, row): return {k: self.decoder.deserialize(v) for k, v in row.items()}

    def get_paginator(self, operation):
        if operation != 'query': raise ValueError('unexpected fixture operation')
        config = get_session().get_paginator_model('dynamodb').get_paginator('Query')
        return Paginator(self.query, config, self.model.operation_model('Query'))

    def transact_get_items(self, **args):
        validate_parameters(args, self.model.operation_model('TransactGetItems').input_shape)
        self.calls.append(('get', deepcopy(args)))
        keys = [self.decode(x['Get']['Key']) for x in args['TransactItems']]
        if any(x['pk'] == CONTROL_KEY for x in keys):
            self.control_reads += 1
            if self.control_reads == 2 and self.on_second_control: self.on_second_control()
        result = []
        for key in keys:
            row = self.items.get((key['pk'], key.get('sk')))
            result.append({'Item': self.encode(row)} if row is not None else {})
        return {'Responses': result}

    def query(self, **args):
        validate_parameters(args, self.model.operation_model('Query').input_shape)
        self.calls.append(('query', deepcopy(args)))
        values = self.decode(args['ExpressionAttributeValues'])
        pk, prefix = (values[':owner'], values[':meal']) if ':owner' in values else (values[':pk'], values[':event'])
        after = self.decode(args['ExclusiveStartKey'])['sk'] if args.get('ExclusiveStartKey') else ''
        rows = [row for (p, sk), row in sorted(self.items.items(), key=lambda x: str(x[0]))
                if p == pk and type(sk) is str and sk.startswith(prefix) and sk > after]
        selected = rows[:min(args['Limit'], self.page_size)]
        result = {'Items': [self.encode(x) for x in selected]}
        if len(rows) > len(selected):
            result['LastEvaluatedKey'] = self.encode({'pk': pk, 'sk': selected[-1]['sk']})
        if self.bad_cursor is not None: result['LastEvaluatedKey'] = self.encode(self.bad_cursor)
        return result

    def transact_write_items(self, **args):
        validate_parameters(args, self.model.operation_model('TransactWriteItems').input_shape)
        self.calls.append(('check', deepcopy(args)))
        if self.write_error: raise self.write_error
        return {}


class CombiHistoryTests(unittest.TestCase):
    def setUp(self):
        self.owner = 'synthetic_owner'
        self.client = HistoryClient(self.owner)
        self.authority = DynamoCombiPriorHistoryAuthority(auth_table='foodtech-auth-sessions-dev',
            cooking_table='foodtech-cooking-jobs-dev', account_id='123456789012', client=self.client, clock=lambda: NOW)
        self.meals = [uid('older'), uid('prior'), uid('current')]
        for meal, day in zip(self.meals, (1, 2, 3)): self.add_meal(meal, day)
        current, prior = (self.meta(self.meals[i]) for i in (2, 1))
        self.binding = CombiExecutionBinding('RICE', self.meals[2], 4, current['rice_cooking_job_id'], current['source']['raw_sha256'],
            self.meals[1], 4, prior['rice_cooking_job_id'], prior['source']['raw_sha256'], 2)
        coverage = {'state': 'ACTIVE', 'version': 2, 'generation': uid('generation'), 'writer_contract': HISTORY_CONTRACT,
            'coverage_scope': COVERAGE_SCOPE, 'coverage_revision': 12, 'baseline_roster_sha256': roster_digest(self.meals),
            'proof_sha256': 'b'*64, 'writer_manifest_sha256': 'a'*64}
        self.account = {'pk': 'USER#' + self.owner, 'epoch': 2, 'suspended': False, COVERAGE_FIELD: coverage}
        self.writer = {'pk': CONTROL_KEY, 'state': 'ENFORCED', 'version': 3, 'generation': uid('generation'),
            'writer_contract': HISTORY_CONTRACT, 'cooking_table_arn': self.authority.cooking_arn,
            'manifest_sha256': 'a'*64, 'verified_at': NOW-100, 'valid_until': NOW+1000}
        self.history = {'pk': f'SCOPE#USER#{self.owner}#COMBI_MEALS', 'sk': 'HISTORY#REVISION', 'revision': 12}
        for row in (self.account, self.writer, self.history): self.client.items[(row['pk'], row.get('sk'))] = row

    def meta(self, meal): return self.client.items[(f'SCOPE#USER#{self.owner}#COMBI_MEAL#{meal}', 'META')]
    def job(self, meal, menu='RICE'):
        meta = self.meta(meal)
        return self.client.items[(meta['pk'], 'JOB#'+menu+'#'+meta['rice_cooking_job_id' if menu == 'RICE' else 'mackerel_cooking_job_id'])]

    def add_meal(self, meal, day):
        pk = f'SCOPE#USER#{self.owner}#COMBI_MEAL#{meal}'
        meta = {'pk': pk, 'sk': 'META', 'meal_batch_id': meal, 'version': 4, 'provenance': 'SYNTHETIC', 'equipment_size': SIZE,
            'rice_cooking_job_id': uid(meal+'rice'), 'mackerel_cooking_job_id': uid(meal+'fish'), 'mode_transition_id': uid(meal+'transition'),
            'source': {'device_id': 'combi-device-1', 'raw_sha256': hashlib.sha256(meal.encode()).hexdigest()}}
        self.client.items[(pk, 'META')] = meta
        listing_pk = f'SCOPE#USER#{self.owner}#COMBI_MEALS'; sk = 'MEAL#' + meal
        self.client.items[(listing_pk, sk)] = {'pk': listing_pk, 'sk': sk, 'meal_batch_id': meal}
        for menu, field in (('RICE', 'rice_cooking_job_id'), ('MACKEREL', 'mackerel_cooking_job_id')):
            sk = 'JOB#'+menu+'#'+meta[field]
            self.client.items[(pk, sk)] = {'pk': pk, 'sk': sk, 'job_id': meta[field], 'meal_batch_id': meal,
                'menu': menu, 'status': 'UNLOADED', 'active_cooking_started_at': f'2026-01-{day:02d}T10:00:00Z',
                'food_unloaded_at': f'2026-01-{day:02d}T10:30:00Z'}

    def observe(self): return self.authority.observe(owner=self.owner, binding=self.binding)
    def coverage_refresh(self): self.account[COVERAGE_FIELD]['baseline_roster_sha256'] = roster_digest([
        row['meal_batch_id'] for (pk, sk), row in self.client.items.items() if pk == self.history['pk'] and str(sk).startswith('MEAL#')])

    def test_complete_roster_and_revalidation_produce_private_typed_observation(self):
        result = self.observe()
        self.assertEqual(result.binding, self.binding)
        self.assertEqual(result.history_revision, 12)
        self.assertEqual(self.authority.revalidate(result), result)
        self.assertNotIn(self.owner, repr(result))
        self.assertFalse(any(kind == 'check' for kind, _ in self.client.calls))

    def test_multiple_pages_and_1001_meals_do_not_stop_after_the_selected_pair(self):
        for i in range(998): self.add_meal(uid('large-'+str(i)), 1)
        self.coverage_refresh()
        result = self.observe()
        queries = [args for kind, args in self.client.calls if kind == 'query']
        self.assertEqual(len(queries), 21)
        self.assertTrue(all(args['ConsistentRead'] for args in queries))
        self.assertTrue(all(len(args['TransactItems']) <= 100 for kind, args in self.client.calls if kind == 'get'))
        self.assertEqual(result.roster_sha256, self.account[COVERAGE_FIELD]['baseline_roster_sha256'])

    def test_missing_or_unprotected_coverage_never_reads_the_roster(self):
        original = deepcopy(self.account)
        for mutation in ('missing', 'job_contract', 'suspended', 'lock', 'epoch', 'expired', 'bad_integer', 'false_baseline'):
            with self.subTest(mutation=mutation):
                self.account.clear(); self.account.update(deepcopy(original)); self.writer['valid_until'] = NOW+1000
                if mutation == 'missing': self.account.pop(COVERAGE_FIELD)
                elif mutation == 'job_contract': self.account[COVERAGE_FIELD]['writer_contract'] = 'atomic-cooking-history-v1'
                elif mutation == 'suspended': self.account['suspended'] = True
                elif mutation == 'lock': self.account['deletion_lock_request'] = str(uuid4())
                elif mutation == 'epoch': self.account['epoch'] = 3
                elif mutation == 'expired': self.writer['valid_until'] = NOW
                elif mutation == 'bad_integer': self.account[COVERAGE_FIELD]['coverage_revision'] = Decimal('1E100000')
                else: self.account[COVERAGE_FIELD]['coverage_revision'] = 0
                self.client.calls.clear()
                with self.assertRaises(PriorHistoryUnavailable): self.observe()
                self.assertFalse(any(kind == 'query' for kind, _ in self.client.calls))

    def test_attested_empty_baseline_can_cover_subsequent_atomic_writes(self):
        self.account[COVERAGE_FIELD].update(coverage_revision=0, baseline_roster_sha256=roster_digest([]))
        self.assertEqual(self.observe().coverage_revision, 0)

    def test_subsecond_wall_clock_does_not_misclassify_an_already_completed_operation_as_future(self):
        self.authority.clock = lambda: NOW + .5
        self.job(self.meals[2]).update(
            active_cooking_started_at=datetime.fromtimestamp(NOW-.5, timezone.utc).isoformat(),
            food_unloaded_at=datetime.fromtimestamp(NOW+.25, timezone.utc).isoformat())
        self.assertEqual(self.observe().binding, self.binding)

    def test_metadata_source_job_version_or_roster_hash_mismatch_is_not_a_witness(self):
        saved = deepcopy(self.client.items)
        for change in ('version', 'source', 'job', 'missing', 'hash', 'wrong_owner'):
            with self.subTest(change=change):
                self.client.items = deepcopy(saved)
                # Rebind references to the rows used by the fixture client.
                self.account = self.client.items[('USER#'+self.owner, None)]
                current = self.meta(self.meals[2])
                if change == 'version': current['version'] = 5
                elif change == 'source': current['source']['raw_sha256'] = '0'*64
                elif change == 'job': self.job(self.meals[2])['job_id'] = str(uuid4())
                elif change == 'missing': del self.client.items[(current['pk'], 'META')]
                elif change == 'hash': self.account[COVERAGE_FIELD]['baseline_roster_sha256'] = '0'*64
                else: current['pk'] = 'SCOPE#USER#other#COMBI_MEAL#'+self.meals[2]
                with self.assertRaises(PriorHistoryUnavailable): self.observe()

    def test_a_more_recent_same_menu_device_batch_cannot_be_skipped(self):
        meal = uid('intervening'); self.add_meal(meal, 2)
        job = self.job(meal); job.update(active_cooking_started_at='2026-01-02T12:00:00Z', food_unloaded_at='2026-01-02T12:30:00Z')
        self.coverage_refresh()
        with self.assertRaises(PriorHistoryUnavailable): self.observe()
        self.meta(meal)['source']['device_id'] = 'different-device'
        self.assertEqual(self.observe().binding, self.binding)
        self.meta(meal)['source']['device_id'] = 'combi-device-1'
        self.meta(meal)['equipment_size'] = 'LARGE_ROLL_IN_RACK_20'
        self.assertEqual(self.observe().binding, self.binding)

    def test_intervening_operation_after_both_selected_ids_in_pagination_is_still_read(self):
        meal = 'ffffffff-ffff-4fff-8fff-ffffffffffff'
        self.add_meal(meal, 2)
        self.job(meal).update(active_cooking_started_at='2026-01-02T12:00:00Z', food_unloaded_at='2026-01-02T12:30:00Z')
        self.account[COVERAGE_FIELD].update(coverage_revision=1, baseline_roster_sha256=roster_digest([self.meals[0]]))
        self.client.page_size = 1
        with self.assertRaises(PriorHistoryUnavailable): self.observe()
        self.assertEqual(len([1 for kind, _ in self.client.calls if kind == 'query']), 4)

    def test_bounds_never_turn_a_partial_roster_into_a_witness(self):
        with patch('foodtech_business_api.combi_prior_history.MAX_MEALS', 2):
            with self.assertRaises(PriorHistoryUnavailable): self.observe()
        self.client.page_size = 1
        with patch('foodtech_business_api.combi_prior_history.MAX_PAGES', 1):
            with self.assertRaises(PriorHistoryUnavailable): self.observe()

    def test_unknown_active_device_time_or_overlapping_operation_is_not_excluded(self):
        original_meta, original_job = deepcopy(self.meta(self.meals[0])), deepcopy(self.job(self.meals[0]))
        for change in ('device', 'timestamp', 'overlap', 'tie', 'running'):
            with self.subTest(change=change):
                meta, job = self.meta(self.meals[0]), self.job(self.meals[0])
                meta.clear(); meta.update(deepcopy(original_meta)); job.clear(); job.update(deepcopy(original_job))
                if change == 'device': meta['source']['device_id'] = ''
                elif change == 'timestamp': job['active_cooking_started_at'] = None
                elif change == 'overlap': job['food_unloaded_at'] = '2026-01-03T11:00:00Z'
                elif change == 'tie': job.update(active_cooking_started_at='2026-01-02T10:00:00Z', food_unloaded_at='2026-01-02T10:30:00Z')
                else: job.update(status='RUNNING', food_unloaded_at=None)
                with self.assertRaises(PriorHistoryUnavailable): self.observe()

    def test_canonical_pending_job_does_not_invent_a_started_operation(self):
        job = self.job(self.meals[0]); job.update(status='PENDING', active_cooking_started_at=None, food_unloaded_at=None)
        self.meta(self.meals[0]).pop('source')
        self.assertEqual(self.observe().binding, self.binding)

    def test_foreign_or_nonadvancing_cursor_is_rejected(self):
        self.client.page_size = 1
        for pk, sk in [('foreign', 'MEAL#'+self.meals[0]), (self.history['pk'], 'wrong')]:
            self.client.bad_cursor = {'pk': pk, 'sk': sk}
            with self.assertRaises(PriorHistoryUnavailable): self.observe()

    def test_control_or_history_changes_after_reading_invalidate_the_observation(self):
        for field in ('revision', 'coverage', 'writer'):
            with self.subTest(field=field):
                self.client.control_reads = 0
                if field == 'revision': self.client.on_second_control = lambda: self.history.update(revision=13)
                elif field == 'coverage': self.client.on_second_control = lambda: self.account[COVERAGE_FIELD].update(version=3)
                else: self.client.on_second_control = lambda: self.writer.update(version=4)
                with self.assertRaises(PriorHistoryUnavailable): self.observe()
                self.history['revision'] = 12; self.account[COVERAGE_FIELD]['version'] = 2; self.writer['version'] = 3

    def reheat(self, meal, number, start, end):
        job = self.job(meal, 'MACKEREL'); segment = {'segment_id': uid(meal+'reheat'+str(number)), 'started_at': start, 'unloaded_at': end}
        sk = f'EVENT#{number:012d}'
        self.client.items[(self.meta(meal)['pk'], sk)] = {'pk': self.meta(meal)['pk'], 'sk': sk,
            'event': 'MACKEREL_REHEATED', 'segment': segment}
        job.update(latest_reheat=segment, current_unloaded_at=end)

    def fish_binding(self):
        return replace(self.binding, menu='MACKEREL', current_job_id=self.meta(self.meals[2])['mackerel_cooking_job_id'],
                       prior_job_id=self.meta(self.meals[1])['mackerel_cooking_job_id'])

    def test_all_reheat_audits_are_considered_even_when_latest_is_after_the_current_job(self):
        self.binding = self.fish_binding()
        self.client.page_size = 1
        self.reheat(self.meals[0], 1, '2026-01-01T11:00:00Z', '2026-01-01T11:10:00Z')
        self.assertEqual(self.observe().binding, self.binding)
        self.reheat(self.meals[0], 2, '2026-01-02T11:00:00Z', '2026-01-02T11:10:00Z')
        self.reheat(self.meals[0], 3, '2026-01-04T11:00:00Z', '2026-01-04T11:10:00Z')
        with self.assertRaises(PriorHistoryUnavailable): self.observe()

    def test_missing_reheat_audit_cannot_be_replaced_by_a_latest_pointer(self):
        self.binding = self.fish_binding()
        self.reheat(self.meals[0], 1, '2026-01-01T11:00:00Z', '2026-01-01T11:10:00Z')
        del self.client.items[(self.meta(self.meals[0])['pk'], 'EVENT#000000000001')]
        with self.assertRaises(PriorHistoryUnavailable): self.observe()

    def test_witness_and_gateway_condition_roundtrip_preserve_the_same_protected_records(self):
        from foodtech_ai_gateway.combi_history_conditions import DynamoCombiHistoryConditions
        from foodtech_ai_gateway.policy import Execution, Feature
        witness = self.observe()
        execution = Execution(str(uuid4()), self.owner, Feature.RECIPE_CORRECTION, 'llama3.1:8b', None, 'SYNTHETIC', Decimal(0),
            job_id=self.binding.current_job_id, job_version=self.binding.current_meal_version,
            prior_job_id=self.binding.prior_job_id, prior_job_version=self.binding.prior_meal_version, combi_binding=self.binding)
        conditions = DynamoCombiHistoryConditions(witness, auth_table=self.authority.auth,
            cooking_table=self.authority.cooking, account_id='123456789012', client=self.client, clock=lambda: NOW)
        before = deepcopy(self.client.items)
        self.assertTrue(conditions.verify(execution))
        self.assertEqual(self.client.items, before)
        checks = self.client.calls[-1][1]['TransactItems']
        auth_values = self.client.decode(checks[0]['ConditionCheck']['ExpressionAttributeValues'])
        self.assertEqual(auth_values[':ch_manifest'], self.account[COVERAGE_FIELD]['writer_manifest_sha256'])
        self.assertEqual(auth_values[':ch_revision'], self.account[COVERAGE_FIELD]['coverage_revision'])
        self.assertEqual(self.client.decode(checks[2]['ConditionCheck']['ExpressionAttributeValues'])[':revision'], self.history['revision'])

    def test_huge_fractional_boolean_and_nonfinite_numbers_are_bounded_before_conversion(self):
        for value in (True, Decimal('1E100000'), Decimal('NaN'), Decimal('1.5'), 0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError): history_integer(value)


if __name__ == '__main__':
    unittest.main()
