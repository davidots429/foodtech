import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException

from foodtech_business_api.combi_quality_aggregate import refresh_pending_for_subject
from foodtech_business_api.cooking_store import Conflict, MemoryCookingStore
from foodtech_contracts.combi_quality_load import RULE_VERSION


class RefreshCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryCookingStore()
        self.meal_id = UUID('00000000-0000-4000-8000-000000000001')
        self.pk = f'SCOPE#USER#customer#COMBI_MEAL#{self.meal_id}'
        self.store.create_meal(self.pk, [{'sk': 'META', 'version': 2, 'meal_batch_id': str(self.meal_id)}, {'sk': 'JOB'}])
        self.marker_key = (self.pk, 'MEAL_AGGREGATE#DIRTY')
        self.store.items[self.marker_key] = {'pk': self.pk, 'sk': self.marker_key[1], 'version': 2, 'state': 'PENDING'}
        self.ids = [f'{i:064x}' for i in range(1, 1002)]
        self.calls = []

    async def page(self, _store, _subject, _meal_id, limit, cursor):
        ids = [group for group in self.ids if cursor is None or group > cursor]
        shown = ids[:limit]
        return {'items': [{'group_id': group, 'selection': {'load_rule_version': RULE_VERSION,
            'meal_ids': [str(self.meal_id)], 'menu': 'RICE', 'equipment_size': 'MEDIUM_FIXED_RACK_10',
            'recipe_version': group, 'load_band': 'PARTIAL', 'period_start': '2026-11-01T00:00:00Z',
            'period_end': '2026-12-01T00:00:00Z'}} for group in shown],
            'next_cursor': shown[-1] if len(ids) > limit else None}

    async def aggregate(self, _store, _subject, _epoch, body):
        self.calls.append(body.recipe_version)
        return {'group_id': body.recipe_version}

    async def test_more_than_1000_groups_resume_without_repeating_completed_chunk(self):
        with patch('foodtech_business_api.combi_quality_aggregate.list_meal_dependencies_for_subject', side_effect=self.page), \
             patch('foodtech_business_api.combi_quality_aggregate.create_meal_aggregate_for_subject', side_effect=self.aggregate):
            first = await refresh_pending_for_subject(self.store, 'customer', 0, self.meal_id)
            self.assertEqual(first, {'status': 'PARTIAL', 'refreshed_group_count': 1000, 'meal_version': 2})
            self.assertEqual(self.store.items[self.marker_key]['cursor'], self.ids[999])
            second = await refresh_pending_for_subject(self.store, 'customer', 0, self.meal_id)
            self.assertEqual(second, {'status': 'REFRESHED', 'refreshed_group_count': 1, 'meal_version': 2})
        self.assertEqual(self.calls, self.ids)
        self.assertNotIn(self.marker_key, self.store.items)

    async def test_newer_meal_version_prevents_stale_checkpoint(self):
        def changed_before_checkpoint(*args):
            self.store.items[(self.pk, 'META')]['version'] = 3
            self.store.items[self.marker_key]['version'] = 3
            raise Conflict
        with patch('foodtech_business_api.combi_quality_aggregate.list_meal_dependencies_for_subject', side_effect=self.page), \
             patch('foodtech_business_api.combi_quality_aggregate.create_meal_aggregate_for_subject', side_effect=self.aggregate), \
             patch.object(self.store, 'checkpoint_meal_aggregate_dirty', side_effect=changed_before_checkpoint):
            with self.assertRaises(HTTPException) as error:
                await refresh_pending_for_subject(self.store, 'customer', 0, self.meal_id)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.store.items[self.marker_key]['version'], 3)
        self.assertNotIn('cursor', self.store.items[self.marker_key])

    async def test_concurrent_progress_and_new_version_do_not_reuse_checkpoint(self):
        first, second = self.ids[49], self.ids[99]
        self.store.checkpoint_meal_aggregate_dirty(self.pk, 2, 'customer', 0, None, first)
        with self.assertRaises(Conflict):
            self.store.acknowledge_meal_aggregate_dirty(self.pk, 2, 'customer', 0, None)
        self.store.checkpoint_meal_aggregate_dirty(self.pk, 2, 'customer', 0, first, second)
        with self.assertRaises(Conflict):
            self.store.checkpoint_meal_aggregate_dirty(self.pk, 2, 'customer', 0, first, self.ids[149])
        self.store.advance_meal(self.pk, 2, {'sk': 'META', 'version': 3, 'meal_batch_id': str(self.meal_id)},
                                {'sk': 'JOB'}, {'sk': 'EVENT#3'})
        self.assertEqual(self.store.items[self.marker_key]['version'], 3)
        self.assertNotIn('cursor', self.store.items[self.marker_key])
        with self.assertRaises(Conflict):
            self.store.acknowledge_meal_aggregate_dirty(self.pk, 2, 'customer', 0, second)
