from copy import deepcopy
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from foodtech_business_api import cooking, combi_ai
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.combi_owned_observations import CombiObservationUnavailable
from foodtech_business_api.main import app


class CombiAiRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = SimpleNamespace(subject='customer-1', epoch=3, surface='customer', csrf='csrf',
                                       expires=int(time.time()) + 300)
        self.store = MemoryCookingStore()
        self.ids, self.jobs = [str(uuid4()) for _ in range(2)], [str(uuid4()) for _ in range(2)]
        self.batches = {}
        for i, meal in enumerate(self.ids):
            pk = f'SCOPE#USER#customer-1#COMBI_MEAL#{meal}'
            self.store.items[(pk, 'META')] = {'pk': pk, 'sk': 'META', 'version': 10 + i,
                                             'source': {'raw_sha256': 'ab'[i] * 64}}
            self.batches[meal] = SimpleNamespace(meal_id=meal, job_id=self.jobs[i], meal_version=10+i,
                source_digest='ab'[i]*64, unresolved=(), baseline=SimpleNamespace(
                    safety_basis_valid=True, process_interrupted=False, size='MEDIUM_FIXED_RACK_10'))
        self.principal = {'pk': 'USER#customer-1', 'epoch': 3}
        self.auth = SimpleNamespace(principal_record=Mock(side_effect=lambda _: deepcopy(self.principal)))
        self.remote = AsyncMock(return_value={'execution_id': str(uuid4()), 'state': 'BLOCKED'})
        self.patches = [patch.dict(os.environ, {'FOODTECH_SECURE_COOKIES': 'false'}),
            patch.object(cooking, '_session', AsyncMock(side_effect=lambda *args: self.session)),
            patch.object(cooking, 'get_cooking_store', return_value=self.store),
            patch.object(combi_ai, '_session', AsyncMock(side_effect=lambda *args: self.session)),
            patch.object(combi_ai, 'get_store', return_value=self.auth),
            patch('foodtech_business_api.recipe_scope.get_store', return_value=self.auth),
            patch.object(combi_ai, '_recipe_gateway', self.remote),
            patch.object(combi_ai, 'read_owned_combi_observation', side_effect=lambda *a, **k: self.batches[k['meal_id']])]
        for p in self.patches: p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.body = {'menu': 'RICE', 'meal_id': self.ids[0], 'meal_version': 10,
                     'prior_meal_id': self.ids[1], 'prior_meal_version': 11}
        self.headers = {'X-CSRF-Token': 'csrf'}
        self.path = '/api/customer/ai/combi/recipe-requests'

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches): p.stop()

    async def test_owned_observations_derive_all_gateway_references(self):
        result = await self.client.post(self.path, json=self.body, headers=self.headers)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.headers['cache-control'], 'no-store')
        self.assertFalse(result.json()['execution_available'])
        path, sent = self.remote.call_args.args
        self.assertEqual(path, '/internal/ai/recipe-requests')
        self.assertEqual(sent['tenant_id'], 'customer-1')
        self.assertEqual(sent['job_id'], self.jobs[0])
        self.assertEqual(sent['combi_binding']['current_source_sha256'], 'a'*64)
        self.assertEqual(sent['combi_binding']['auth_epoch'], 3)

    async def test_csrf_identity_injection_same_meal_and_extra_source_payload_are_rejected(self):
        self.assertEqual((await self.client.post(self.path, json=self.body)).status_code, 403)
        for changes in ({'tenant_id': 'foreign'}, {'prior_meal_id': self.ids[0]},
                        {'source_digest': 'c'*64}, {'meal_version': True}, {'menu': 'JEYUK'}):
            result = await self.client.post(self.path, json={**self.body, **changes}, headers=self.headers)
            self.assertEqual(result.status_code, 422, result.text)
        self.remote.assert_not_called()

    async def test_unknown_owner_missing_source_and_changed_version_never_forward(self):
        for changes in ({'meal_id': str(uuid4())}, {'meal_version': 9}):
            result = await self.client.post(self.path, json={**self.body, **changes}, headers=self.headers)
            self.assertEqual(result.status_code, 409, result.text)
        self.remote.assert_not_called()

    async def test_invalid_observations_missing_quality_or_incomparable_profile_never_forward(self):
        original = self.batches[self.ids[0]]
        self.batches[self.ids[0]] = SimpleNamespace(**{**vars(original), 'unresolved': ('CURRENT_QUALITY_UNAVAILABLE',)})
        self.assertEqual((await self.client.post(self.path, json=self.body, headers=self.headers)).status_code, 409)
        self.batches[self.ids[0]] = SimpleNamespace(**{**vars(original), 'baseline': SimpleNamespace(
            size='LARGE_ROLL_IN_RACK_20', safety_basis_valid=True, process_interrupted=False)})
        self.assertEqual((await self.client.post(self.path, json=self.body, headers=self.headers)).status_code, 409)
        combi_ai.read_owned_combi_observation.side_effect = CombiObservationUnavailable('private detail')
        result = await self.client.post(self.path, json=self.body, headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertNotIn('private detail', result.text)
        self.remote.assert_not_called()

    async def test_session_change_after_gateway_does_not_return_success(self):
        async def changed(*args):
            self.session = SimpleNamespace(**{**vars(self.session), 'epoch': 4})
            return {'execution_id': str(uuid4()), 'state': 'BLOCKED'}
        self.remote.side_effect = changed
        result = await self.client.post(self.path, json=self.body, headers=self.headers)
        self.assertEqual(result.status_code, 401, result.text)

    async def test_preview_and_confirmation_recheck_and_forward_same_owned_binding(self):
        parent, quote = str(uuid4()), str(uuid4())
        preview = await self.client.post(self.path + '/' + parent + '/local-preview', json=self.body, headers=self.headers)
        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertEqual(self.remote.call_args.args[1]['parent_execution_id'], parent)
        result = await self.client.post('/api/customer/ai/combi/recipe-quotes/' + quote + '/confirm',
            json={**self.body, 'notice_version': 'local-recipe-downgrade-v1', 'quality_warning_acknowledged': True},
            headers=self.headers)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(self.remote.call_args.args[1]['combi_binding']['prior_meal_id'], self.ids[1])


if __name__ == '__main__':
    unittest.main()
