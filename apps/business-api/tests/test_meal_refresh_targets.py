import asyncio
import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from foodtech_business_api import meal_refresh_runtime as runtime
from foodtech_business_api.combi_refresh_worker import parse_refresh_targets, refresh_targets
from foodtech_business_api.cooking_store import MemoryCookingStore, meal_aggregate_shard
from foodtech_business_api.combi_quality_aggregate import MealAggregateRequest, create_meal_aggregate_for_subject


SUBJECT = 'synthetic-customer'
MEAL = '00000000-0000-4000-8000-000000000001'
RAW = json.dumps([{'subject': SUBJECT, 'meal_id': MEAL}])
PK = f'SCOPE#USER#{SUBJECT}#COMBI_MEAL#{MEAL}'
TARGETS = parse_refresh_targets(RAW)


class MealRefreshTargetTests(unittest.TestCase):
    def test_invalid_selection_is_rejected_before_any_credential_discovery(self):
        bad = [None, '', '{}', '[]', '[null]', RAW[:-1] + ',' + RAW[1:],
            json.dumps([{'subject': SUBJECT, 'meal_id': MEAL, 'extra': True}]),
            RAW.replace(SUBJECT, 'bad#subject'), RAW.replace(MEAL, MEAL.upper().replace('0001', 'ABCD')),
            '[{"subject":"first","subject":"second","meal_id":"' + MEAL + '"}]',
            json.dumps([{'subject': str(i), 'meal_id': MEAL} for i in range(17)]),
            ' ' * 8193, '[' * 2000 + ']' * 2000]
        for raw in bad:
            env = {'FOODTECH_MEAL_REFRESH_MODE': 'targets', 'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true'}
            if raw is not None:
                env['FOODTECH_MEAL_REFRESH_TARGETS'] = raw
            with self.subTest(raw_type=type(raw).__name__), patch.dict(os.environ, env, clear=True), \
                    patch.object(runtime, 'configured_worker') as factory, redirect_stderr(io.StringIO()):
                self.assertEqual(runtime.main(), 2)
                factory.assert_not_called()

    def test_ambiguous_mode_or_non_strict_manual_mode_cannot_fall_back_to_sweep(self):
        for env in ({'FOODTECH_MEAL_REFRESH_TARGETS': RAW},
                    {'FOODTECH_MEAL_REFRESH_MODE': 'sweep', 'FOODTECH_MEAL_REFRESH_TARGETS': ''},
                    {'FOODTECH_MEAL_REFRESH_MODE': 'typo'},
                    {'FOODTECH_MEAL_REFRESH_MODE': 'targets', 'FOODTECH_MEAL_REFRESH_TARGETS': RAW,
                     'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'false'}):
            with patch.dict(os.environ, env, clear=True), patch.object(runtime, 'configured_worker') as factory, \
                    patch.object(runtime, 'sweep_pending') as sweep, redirect_stderr(io.StringIO()):
                self.assertEqual(runtime.main(), 2)
                factory.assert_not_called()
                sweep.assert_not_called()

    def test_runtime_dispatch_is_explicit_and_emits_counts_only(self):
        result = {'processed': 1, 'partial': 0, 'failed': 0, 'pages': 1}
        output = io.StringIO()
        with patch.dict(os.environ, {'FOODTECH_MEAL_REFRESH_MODE': 'targets',
                'FOODTECH_MEAL_REFRESH_TARGETS': RAW, 'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true'}, clear=True), \
                patch.object(runtime, 'configured_worker', return_value=('store', 'auth')), \
                patch.object(runtime, 'refresh_targets', return_value=result) as selected, \
                patch.object(runtime, 'sweep_pending') as sweep, redirect_stdout(output):
            self.assertEqual(runtime.main(), 0)
        selected.assert_called_once_with('store', 'auth', TARGETS)
        sweep.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), result)
        self.assertNotIn(SUBJECT, output.getvalue())

    def store(self):
        store = MagicMock()
        row = {**TARGETS[0], 'aggregate_refresh_shard': meal_aggregate_shard(PK)}
        store.table.query.return_value = {'Items': [row]}
        store.read_meal_aggregate_dirty.return_value = row
        return store, row

    def test_exact_partition_and_shard_are_queried_and_partial_is_preserved(self):
        store, _ = self.store()
        with patch('foodtech_business_api.combi_refresh_worker.process_pending_marker',
                return_value={'status': 'PARTIAL'}) as process:
            self.assertEqual(refresh_targets(store, object(), TARGETS),
                             {'processed': 1, 'partial': 1, 'failed': 0, 'pages': 1})
        args = store.table.query.call_args.kwargs
        condition = args['KeyConditionExpression'].get_expression()['values']
        self.assertEqual([x.get_expression()['values'][0].name for x in condition], ['aggregate_refresh_shard', 'pk'])
        self.assertEqual([x.get_expression()['values'][1] for x in condition], [meal_aggregate_shard(PK), PK])
        process.assert_called_once()
        self.assertEqual(process.call_args.args[2], TARGETS[0])

    def test_out_of_scope_index_response_or_pending_index_miss_never_processes(self):
        for kind in ('foreign', 'other_sk', 'index_shard', 'duplicate', 'cursor', 'miss', 'base_scope', 'base_shard'):
            store, row = self.store()
            if kind == 'foreign': store.table.query.return_value = {'Items': [{**row, 'pk': 'foreign'}]}
            if kind == 'other_sk': store.table.query.return_value = {'Items': [{**row, 'sk': 'META'}]}
            if kind == 'index_shard': store.table.query.return_value = {'Items': [{**row, 'aggregate_refresh_shard': 'bad'}]}
            if kind == 'duplicate': store.table.query.return_value = {'Items': [row, row]}
            if kind == 'cursor': store.table.query.return_value['LastEvaluatedKey'] = {'pk': PK}
            if kind == 'miss': store.table.query.return_value = {'Items': []}
            if kind == 'base_scope': store.read_meal_aggregate_dirty.return_value = {**row, 'pk': 'foreign'}
            if kind == 'base_shard': store.read_meal_aggregate_dirty.return_value = {**row, 'aggregate_refresh_shard': 'bad'}
            with self.subTest(kind=kind), patch('foodtech_business_api.combi_refresh_worker.process_pending_marker') as process:
                self.assertEqual(refresh_targets(store, object(), TARGETS)['failed'], 1)
                process.assert_not_called()

    def test_stale_index_or_completed_target_uses_the_existing_account_and_marker_checks(self):
        for rows in ([], [{'pk': PK, 'sk': 'MEAL_AGGREGATE#DIRTY', 'aggregate_refresh_shard': meal_aggregate_shard(PK)}]):
            store, _ = self.store()
            store.table.query.return_value = {'Items': rows}
            store.read_meal_aggregate_dirty.return_value = None
            with patch('foodtech_business_api.combi_refresh_worker.process_pending_marker',
                    return_value={'status': 'NO_PENDING'}) as process:
                self.assertEqual(refresh_targets(store, object(), TARGETS)['processed'], 1)
                process.assert_called_once()

    def test_timeout_escapes_while_per_record_failure_remains_retryable(self):
        store, _ = self.store()
        for failure in (TimeoutError('private'), runtime.MealRefreshDeadline('private')):
            with patch('foodtech_business_api.combi_refresh_worker.process_pending_marker', side_effect=failure):
                with self.assertRaises(type(failure)):
                    refresh_targets(store, object(), TARGETS)
        with patch('foodtech_business_api.combi_refresh_worker.process_pending_marker', side_effect=RuntimeError('private')):
            self.assertEqual(refresh_targets(store, object(), TARGETS)['failed'], 1)
        store.table.query.reset_mock()
        with self.assertRaises(ValueError):
            refresh_targets(store, object(), TARGETS + TARGETS)
        store.table.query.assert_not_called()

    def test_real_aggregate_refresh_preserves_another_pending_meal_and_customer(self):
        store, auth = MemoryCookingStore(), MagicMock()
        store.require_principal = True
        auth.get_item.return_value = {'Item': {'epoch': 0}}
        class Index:
            def query(self, **kwargs):
                conditions = kwargs['KeyConditionExpression'].get_expression()['values']
                pk = conditions[1].get_expression()['values'][1]
                dirty = store.read_meal_aggregate_dirty(pk)
                return {'Items': [{**dirty, 'pk': pk}] if dirty else []}
        store.table = Index()
        pks, groups = [], []
        for subject, meal in ((SUBJECT, MEAL), (SUBJECT, MEAL[:-1] + '2'), ('other-customer', MEAL)):
            pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{meal}'
            pks.append(pk)
            meta = {'sk': 'META', 'version': 1, 'meal_batch_id': meal, 'rice_cooking_job_id': 'rice',
                    'mode_transition_id': 'transition', 'mackerel_cooking_job_id': 'fish',
                    'equipment_size': 'MEDIUM_FIXED_RACK_10'}
            store.create_meal(pk, [meta, {'sk': 'JOB#RICE#rice'}, {'sk': 'TRANSITION#transition'},
                                   {'sk': 'JOB#MACKEREL#fish', 'safety_status': 'NOT_MEASURED'}])
            request = MealAggregateRequest.model_validate({'meal_ids': [meal], 'menu': 'MACKEREL',
                'equipment_size': 'MEDIUM_FIXED_RACK_10', 'recipe_version': 'synthetic', 'load_band': 'PARTIAL',
                'period_start': '2026-11-01T00:00:00Z', 'period_end': '2026-12-01T00:00:00Z'})
            aggregate = asyncio.run(create_meal_aggregate_for_subject(store, subject, 0, request))
            groups.append(aggregate['group_id'])
            store.advance_meal(pk, 1, {**meta, 'version': 2}, {'sk': 'JOB#RICE#rice'}, {'sk': 'EVENT#2'})
        # A meal refresh also writes its dependent aggregate and group listing.
        # Other meals, customers and unrelated groups must remain byte-for-byte.
        def selected(key):
            return key[0] in {PK, f'SCOPE#USER#{SUBJECT}#MEAL_AGGREGATE#{groups[0]}'} or \
                (key[0] == f'SCOPE#USER#{SUBJECT}#MEAL_AGGREGATE_GROUPS' and groups[0] in key[1])
        before = {k: copy.deepcopy(v) for k, v in store.items.items() if not selected(k)}
        self.assertEqual(refresh_targets(store, auth, TARGETS), {'processed': 1, 'partial': 0, 'failed': 0, 'pages': 1})
        self.assertIsNone(store.read_meal_aggregate_dirty(PK))
        self.assertEqual({k: v for k, v in store.items.items() if not selected(k)}, before)
        self.assertTrue(all(store.read_meal_aggregate_dirty(pk) for pk in pks[1:]))
        auth.get_item.assert_called_once_with(Key={'pk': f'USER#{SUBJECT}'}, ConsistentRead=True)


if __name__ == '__main__':
    unittest.main()
