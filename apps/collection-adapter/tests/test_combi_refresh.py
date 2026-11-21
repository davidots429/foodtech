import unittest
from unittest.mock import AsyncMock, patch

import httpx

from foodtech_collection_adapter.combi_import import _keep_session_alive


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_retries_then_uses_server_expiry(self):
        request = httpx.Request('POST', 'https://example.test/api/customer/session/refresh')
        api = AsyncMock()
        api.post.side_effect = [httpx.Response(409, request=request),
                                httpx.Response(200, request=request, json={'expires_in': 500}),
                                RuntimeError('stop')]
        with patch('foodtech_collection_adapter.combi_import.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                await _keep_session_alive(api, 'csrf', 400)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [100, 10, 200])
        self.assertEqual(api.post.await_args_list[0].kwargs['headers']['X-CSRF-Token'], 'csrf')

    async def test_unauthorized_stops_import(self):
        request = httpx.Request('POST', 'https://example.test/api/customer/session/refresh')
        api = AsyncMock()
        api.post.return_value = httpx.Response(401, request=request)
        with patch('foodtech_collection_adapter.combi_import.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(httpx.HTTPStatusError):
                await _keep_session_alive(api, 'csrf', 400)

    async def test_repeated_conflict_stops_import(self):
        request = httpx.Request('POST', 'https://example.test/api/customer/session/refresh')
        api = AsyncMock()
        api.post.return_value = httpx.Response(409, request=request)
        with patch('foodtech_collection_adapter.combi_import.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, 'repeatedly conflicted'):
                await _keep_session_alive(api, 'csrf', 400)
        self.assertEqual(api.post.await_count, 6)
