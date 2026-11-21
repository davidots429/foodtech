"""Exercise real aggregate calculations across simulated index pages and retries."""
import asyncio
import unittest
from uuid import UUID

from foodtech_business_api.combi_quality_aggregate import MealAggregateRequest, create_meal_aggregate_for_subject
from foodtech_business_api.combi_refresh_worker import sweep_pending
from foodtech_business_api.cooking_store import MemoryCookingStore, meal_aggregate_shard


class MemoryIndex:
    def __init__(self, store):
        self.store = store

    def query(self, **kwargs):
        shard = kwargs['KeyConditionExpression'].get_expression()['values'][1]
        rows = sorted(({'pk': pk, 'sk': sk, 'aggregate_refresh_shard': row['aggregate_refresh_shard']}
                       for (pk, sk), row in self.store.items.items()
                       if sk == 'MEAL_AGGREGATE#DIRTY' and row['aggregate_refresh_shard'] == shard),
                      key=lambda row: row['pk'])
        cursor = kwargs.get('ExclusiveStartKey')
        if cursor:
            rows = [row for row in rows if row['pk'] > cursor['pk']]
        page, limit = rows[:kwargs['Limit']], kwargs['Limit']
        return {'Items': page, **({'LastEvaluatedKey': page[-1]} if len(rows) > limit else {})}


class AuthTable:
    def __init__(self):
        self.locked = set()

    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead
        return {'Item': {'epoch': 0, **({'deletion_lock_request': 'locked'} if Key['pk'] in self.locked else {})}}


class SweepIntegrationTests(unittest.TestCase):
    def test_same_shard_pagination_failure_and_next_sweep_retry(self):
        store, auth = MemoryCookingStore(), AuthTable()
        store.table = MemoryIndex(store)
        ids = [str(UUID(int=number)) for number in range(1, 100)]
        pairs = [(a, b) for a in ids for b in ids if a != b and
                 meal_aggregate_shard(f'SCOPE#USER#test-customer#COMBI_MEAL#{a}') ==
                 meal_aggregate_shard(f'SCOPE#USER#test-customer#COMBI_MEAL#{b}')]
        meal_ids = sorted(pairs[0])
        for meal_id in meal_ids:
            pk = f'SCOPE#USER#test-customer#COMBI_MEAL#{meal_id}'
            meta = {'sk': 'META', 'version': 1, 'meal_batch_id': meal_id,
                    'rice_cooking_job_id': 'rice', 'mode_transition_id': 'transition',
                    'mackerel_cooking_job_id': 'fish', 'equipment_size': 'MEDIUM_FIXED_RACK_10'}
            store.create_meal(pk, [meta, {'sk': 'JOB#RICE#rice'}, {'sk': 'TRANSITION#transition'},
                                   {'sk': 'JOB#MACKEREL#fish', 'safety_status': 'NOT_MEASURED'}])
            request = MealAggregateRequest.model_validate({'meal_ids': [meal_id], 'menu': 'MACKEREL',
                'equipment_size': 'MEDIUM_FIXED_RACK_10', 'recipe_version': 'synthetic', 'load_band': 'PARTIAL',
                'period_start': '2026-11-01T00:00:00Z', 'period_end': '2026-12-01T00:00:00Z'})
            asyncio.run(create_meal_aggregate_for_subject(store, 'test-customer', 0, request))
            store.advance_meal(pk, 1, {**meta, 'version': 2}, {'sk': 'JOB#RICE#rice'}, {'sk': 'EVENT#2'})
        # One customer owns both records: fail just the first refresh to model a transient write failure.
        original = store.save_meal_aggregate
        failures = [True]
        def fail_once(*args):
            if failures[0]:
                failures[0] = False
                raise RuntimeError('transient failure')
            return original(*args)
        store.save_meal_aggregate = fail_once
        first = sweep_pending(store, auth, page_size=1)
        self.assertEqual((first['processed'], first['failed']), (1, 1))
        pending = [pk for (pk, sk) in store.items if sk == 'MEAL_AGGREGATE#DIRTY']
        self.assertEqual(len(pending), 1)
        second = sweep_pending(store, auth, page_size=1)
        self.assertEqual((second['processed'], second['failed']), (1, 0))
        self.assertFalse(any(sk == 'MEAL_AGGREGATE#DIRTY' for _, sk in store.items))
        third = sweep_pending(store, auth, page_size=1)
        self.assertEqual((third['processed'], third['failed']), (0, 0))
