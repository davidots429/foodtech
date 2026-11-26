"""Synthetic lease delivery; no AWS requests or production invocation route."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx

from foodtech_business_api.recipe_read_broker import RecipeReadLease
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
