import copy
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4
import httpx

from test_recipe_status import RecipeStatusFixtures
from foodtech_business_api.main import app
from foodtech_business_api.recipe_review_store import RecipeReviewStore
from foodtech_contracts.recipe_result_view import recipe_proposal_digest


class RecipeReviewApiTests(RecipeStatusFixtures):
    def setUp(self):
        super().setUp()
        self.store.items[self.pk, 'META'].update(owner_subject=self.subject, menu='JEYUK', safety_status='PASSED')
        self.state = 'SUCCEEDED'
        self.proposal = {'status': 'NEEDS_REVIEW', 'schema_version': 'synthetic-recommendation-output-v1',
            'guard_version': 'synthetic-recommendation-guard-v1', 'menu': 'JEYUK', 'data': {
                'temperature_c': '200', 'rpm': '60', 'duration_seconds': 300, 'load_kg': '7',
                'vegetable_interval_seconds': 60, 'sauce_interval_seconds': 120}}
        raw = {k: v for k, v in self.proposal.items() if k != 'menu'}
        raw.update(error_code=None, fallback_used=False, attempts=1)
        self.result = {'state': self.state, 'response_text': json.dumps(raw), 'credential': 'private-marker'}
        self.body = {'job_id': self.job_id, 'expected_job_version': 3, 'expected_review_version': 0,
            'request_id': str(uuid4()), 'proposal_digest': recipe_proposal_digest(self.proposal),
            'decision': 'APPROVE', 'reason': '합성 조건을 확인했습니다.', 'acknowledged': True}

    async def request(self, method='GET', body=None, csrf='csrf', path=None, params=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.request(method, path or f'/api/customer/ai/recipe-executions/{self.execution_id}/review',
                params=params if params is not None else {'job_id': self.job_id}, json=body,
                headers={'X-CSRF-Token': csrf})

    async def test_context_save_and_history_are_bound_to_saved_result_not_model_execution(self):
        before = copy.deepcopy(self.store.items[self.pk, 'META'])
        context = await self.request()
        self.assertEqual(context.status_code, 200)
        self.assertTrue(context.json()['can_review'])
        self.assertEqual(context.json()['proposal_digest'], self.body['proposal_digest'])
        response = await self.request('POST', self.body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json()['review']['decision'], 'APPROVE')
        self.assertEqual(self.store.items[self.pk, 'META'], before)
        history = await self.request(path='/api/customer/ai/recipe-reviews')
        self.assertEqual(len(history.json()['items']), 1)
        self.assertNotIn('private-marker', history.text)
        self.assertNotIn('response_text', history.text)
        self.registry.create_execution.assert_not_called()

    async def test_csrf_invalid_acknowledgement_and_injected_fields_never_reach_gateway(self):
        self.assertEqual((await self.request('POST', self.body, csrf='wrong')).status_code, 403)
        for changed in ({'acknowledged': False}, {'acknowledged': 1}, {'reason': ' '}, {'tenant_id': 'foreign'}):
            self.assertEqual((await self.request('POST', {**self.body, **changed})).status_code, 422)
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.store.items), 1)

    async def test_stale_digest_changed_job_or_unavailable_proposal_is_not_reviewed(self):
        self.assertEqual((await self.request('POST', {**self.body, 'proposal_digest': '0' * 64})).status_code, 409)
        self.on_response = lambda: self.store.items[self.pk, 'META'].update(version=4)
        self.assertEqual((await self.request('POST', self.body)).status_code, 409)
        self.assertFalse((await self.request()).json()['can_review'])
        self.on_response = None
        self.result['response_text'] = 'private-invalid-marker'
        response = await self.request('POST', self.body)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('private-invalid-marker', response.text)
        self.assertEqual(len(self.store.items), 1)

    async def test_foreign_execution_or_job_cannot_create_a_review(self):
        self.execution = replace(self.execution, tenant_id='foreign')
        self.assertEqual((await self.request('POST', self.body)).status_code, 404)
        self.assertEqual((await self.request('POST', {**self.body, 'job_id': str(uuid4())})).status_code, 404)
        self.assertEqual(len(self.store.items), 1)

    async def test_identical_retry_and_competing_review_use_versions_without_overwrite(self):
        first = await self.request('POST', self.body)
        again = await self.request('POST', self.body)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json(), again.json())
        self.assertEqual((await self.request('POST', {**self.body, 'request_id': str(uuid4())})).status_code, 409)
        second = {**self.body, 'request_id': str(uuid4()), 'expected_review_version': 1,
                  'decision': 'REJECT', 'reason': '조건을 다시 검토해야 합니다.'}
        self.assertEqual((await self.request('POST', second)).status_code, 201)
        page = await self.request(path='/api/customer/ai/recipe-reviews', params={'job_id': self.job_id, 'limit': 1})
        self.assertEqual(page.json()['next_cursor'], 1)
        self.assertEqual(page.json()['items'][0]['decision'], 'APPROVE')
        page = await self.request(path='/api/customer/ai/recipe-reviews', params={'job_id': self.job_id, 'limit': 1, 'cursor': 1})
        self.assertIsNone(page.json()['next_cursor'])
        self.assertEqual(page.json()['items'][0]['decision'], 'REJECT')

    async def test_revocation_during_gateway_read_prevents_review_write(self):
        self.on_response = lambda: setattr(self, 'epoch', 1)
        response = await self.request('POST', self.body)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.store.items), 1)

    async def test_revocation_after_commit_hides_response_and_allows_history_recovery(self):
        original = RecipeReviewStore.save
        owner = self
        def changed(store, *args):
            result = original(store, *args)
            owner.epoch = 1
            return result
        with patch.object(RecipeReviewStore, 'save', changed):
            response = await self.request('POST', self.body)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('proposal', response.text)
        self.assertEqual(self.epoch, 1)  # The next request uses a fresh permitted session at the current epoch.
        history = await self.request(path='/api/customer/ai/recipe-reviews')
        self.assertEqual(len(history.json()['items']), 1)
        self.assertEqual(history.json()['items'][0]['request_id'], self.body['request_id'])

    async def test_history_rechecks_session_after_storage_read(self):
        self.assertEqual((await self.request('POST', self.body)).status_code, 201)
        original = RecipeReviewStore.history
        owner = self
        def changed(store, *args):
            result = original(store, *args)
            owner.epoch = 1
            return result
        with patch.object(RecipeReviewStore, 'history', changed):
            response = await self.request(path='/api/customer/ai/recipe-reviews')
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('proposal', response.text)
