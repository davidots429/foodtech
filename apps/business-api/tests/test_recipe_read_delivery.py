"""Synthetic lease delivery; no AWS requests or production invocation route."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx

from foodtech_business_api.recipe_read_broker import RecipeReadLease, bind_authenticated_recipe_history
from foodtech_business_api.recipe_scope import RecipeScopeDenied
from fastapi import HTTPException
from types import SimpleNamespace
from foodtech_business_api.prior_history_authority import DynamoPriorHistoryAuthority, PriorHistoryUnavailable
from foodtech_contracts.prior_history_witness import PriorHistoryWitness
from foodtech_business_api.recipe_read_delivery import RecipeDeliveryUnavailable, deliver_internal_recipe_read_lease
from foodtech_contracts.recipe_read_handoff import CONTENT_TYPE, RecipeReadHandoff
from foodtech_contracts.recipe_read_scope import RecipeReadScope
from foodtech_contracts.source_read_authorization import SourceReadAuthorization


class RecipeReadDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.scope = RecipeReadScope('opaque_subject', str(uuid4()),
            tuple(f'SCOPE#USER#opaque_subject#JOB#{uuid4()}' for _ in range(2)),
            ('JEYUK#jeyuk-current', 'JEYUK#jeyuk-prior'), (2, 1), (12, 11), 'PENDING', 1, 3)
        self.grant = SourceReadAuthorization('foodtech-auth-sessions-dev', 'foodtech-source-access-dev',
            self.scope.owner, self.scope.execution_id, 3, self.scope.source_partitions,
            (str(uuid4()), str(uuid4())), (1, 1), (int(now.timestamp()) + 900,) * 2)
        session = boto3.Session(region_name='ap-southeast-2', aws_access_key_id='ASIA' + 'A' * 16,
            aws_secret_access_key='synthetic-secret-marker', aws_session_token='synthetic-token-marker')
        self.lease = RecipeReadLease(self.scope, now + timedelta(seconds=899), session, Config(),
            read_role_arn='arn:aws:iam::123456789012:role/foodtech-recipe-source-read-dev',
            assumed_role_arn='arn:aws:sts::123456789012:assumed-role/foodtech-recipe-source-read-dev/recipe-' + 'a' * 32)
        self.lease._bind_authorization(self.grant)
        self.valid = {'execution_id': self.scope.execution_id, 'state': 'DENIED',
                      'response_text': None, 'error_code': 'RECOMMENDATION_EVIDENCE_UNVERIFIED'}

    async def deliver(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('foodtech_business_api.ai._gateway_settings', return_value=('https://localhost:8443', 'ca', 'cert', 'key')), \
             patch('foodtech_business_api.ai._client', return_value=client):
            return await deliver_internal_recipe_read_lease(self.lease)

    async def test_dedicated_path_and_content_type_never_forward_browser_cookies(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.path, f'/internal/ai/recipe-executions/{self.scope.execution_id}/invoke')
            self.assertEqual(request.headers['Content-Type'], CONTENT_TYPE)
            self.assertNotIn('cookie', request.headers)
            self.assertNotIn('authorization', request.headers)
            self.assertEqual(RecipeReadHandoff._decode_from_internal_tls(request.content).scope, self.scope)
            return httpx.Response(200, headers={'Cache-Control': 'no-store'}, json=self.valid)
        result = await self.deliver(handler)
        self.assertEqual(result, self.valid)
        self.assertNotIn('synthetic-secret-marker', json.dumps(result))
        self.assertEqual(len(requests), 1)

    def history_fixture(self):
        current, prior = (key.rsplit('#', 1)[-1] for key in self.scope.job_partitions)
        witness = PriorHistoryWitness(self.scope.owner, current, prior, self.scope.job_versions, self.scope.auth_epoch,
            10, 1, 1, str(uuid4()), 'a' * 64, 'b' * 64, 10, 'a' * 64, 'c' * 64)
        authority = DynamoPriorHistoryAuthority(auth_table='foodtech-auth-sessions-dev',
            cooking_table='foodtech-cooking-jobs-dev', account_id='123456789012', client=object(), clock=lambda: 1000)
        return witness, authority

    async def test_only_revalidated_server_history_crosses_private_delivery(self):
        witness, authority = self.history_fixture()
        with patch.object(authority, 'revalidate', return_value=witness) as observe:
            self.lease._bind_history(witness, authority)
            def handler(request):
                handoff = RecipeReadHandoff._decode_from_internal_tls(request.content)
                self.assertEqual(handoff.history, witness)
                return httpx.Response(200, headers={'Cache-Control': 'no-store'}, json=self.valid)
            result = await self.deliver(handler)
            self.assertEqual(observe.call_count, 2)
            self.assertEqual(result, self.valid)
            self.assertNotIn('history', result)

    async def test_changed_history_after_bind_never_sends_credentials_or_invokes_gateway(self):
        witness, authority = self.history_fixture()
        with patch.object(authority, 'revalidate', side_effect=[witness, PriorHistoryUnavailable('synthetic changed')]):
            self.lease._bind_history(witness, authority)
            def handler(_):
                raise AssertionError('must not send a stale-history lease')
            with self.assertRaises(RecipeDeliveryUnavailable) as error:
                await self.deliver(handler)
            self.assertNotIn('synthetic-secret-marker', str(error.exception))

    async def test_authenticated_history_binding_checks_csrf_and_scope_around_observation(self):
        witness, authority = self.history_fixture()
        with patch('foodtech_business_api.auth._session', AsyncMock(return_value=SimpleNamespace(subject=self.scope.owner))), \
                patch('foodtech_business_api.auth._csrf') as csrf, \
                patch('foodtech_business_api.recipe_read_broker.revalidate_recipe_scope', AsyncMock()) as scope, \
                patch.object(authority, 'observe', return_value=witness) as observe, \
                patch.object(authority, 'revalidate', return_value=witness):
            result = await bind_authenticated_recipe_history(object(), self.lease, authority=authority,
                execution_reader=object(), job_reader=object())
            self.assertIs(result, self.lease)
            csrf.assert_called_once()
            self.assertEqual(scope.await_count, 3)
            self.assertEqual(observe.call_args.kwargs['owner'], self.scope.owner)
            self.assertEqual(self.lease._internal_handoff().history, witness)

    async def test_csrf_or_changed_scope_cannot_attach_history(self):
        for mode in ['csrf', 'scope']:
            witness, authority = self.history_fixture()
            with self.subTest(mode=mode), \
                    patch('foodtech_business_api.auth._session', AsyncMock(return_value=SimpleNamespace(subject=self.scope.owner))), \
                    patch('foodtech_business_api.auth._csrf', side_effect=HTTPException(403) if mode == 'csrf' else None), \
                    patch('foodtech_business_api.recipe_read_broker.revalidate_recipe_scope',
                          AsyncMock(side_effect=[None, RecipeScopeDenied('changed')] if mode == 'scope' else None)), \
                    patch.object(authority, 'observe', return_value=witness) as observe, \
                    patch.object(authority, 'revalidate', return_value=witness) as revalidate:
                with self.assertRaises((HTTPException, RecipeScopeDenied)):
                    await bind_authenticated_recipe_history(object(), self.lease, authority=authority,
                        execution_reader=object(), job_reader=object())
                if mode == 'csrf':
                    observe.assert_not_called()
                revalidate.assert_not_called()
                self.assertIsNone(self.lease._internal_handoff().history)

    async def test_error_bodies_redirects_and_unexpected_response_fields_are_not_returned(self):
        for status, headers, content in [(500, {}, b'synthetic-secret-marker'),
            (307, {'Location': 'https://external.invalid'}, b'synthetic-secret-marker'),
            (200, {'Cache-Control': 'no-store'}, json.dumps({**self.valid, 'credentials': 'synthetic-secret-marker'}).encode()),
            (200, {'Cache-Control': 'no-store'}, b'x' * 24577)]:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, headers=headers, content=content)
            with self.subTest(status=status), self.assertRaisesRegex(RecipeDeliveryUnavailable, '^recipe invocation unavailable$'):
                await self.deliver(handler)
            self.assertEqual(len(calls), 1)

    async def test_lease_without_verified_observation_is_not_sent(self):
        unverified = RecipeReadLease(self.scope, self.lease.expires_at, MagicMock(), Config())
        with patch('foodtech_business_api.ai._gateway_settings') as settings, \
             self.assertRaises(RecipeDeliveryUnavailable):
            await deliver_internal_recipe_read_lease(unverified)
        settings.assert_not_called()

    async def test_sdk_failure_during_export_does_not_expose_details_or_send(self):
        session = MagicMock()
        session.get_credentials.side_effect = ClientError({'Error': {'Code': 'SyntheticFailure',
            'Message': 'synthetic-secret-marker'}}, 'SyntheticOperation')
        broken = RecipeReadLease(self.scope, self.lease.expires_at, session, Config())
        broken._bind_authorization(self.grant)
        with patch('foodtech_business_api.ai._gateway_settings') as settings, \
             self.assertRaisesRegex(RecipeDeliveryUnavailable, '^recipe invocation unavailable$'):
            await deliver_internal_recipe_read_lease(broken)
        settings.assert_not_called()
