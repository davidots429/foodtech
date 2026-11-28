import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException

from foodtech_ai_gateway.tonkatsu_read_handoff import receive_internal_tonkatsu_handoff
from foodtech_contracts.tonkatsu_read_handoff import CONTENT_TYPE


class TonkatsuTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpinned_request_cannot_read_body_or_resolve_execution(self):
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(mtls_listener=True, tonkatsu_broker_listener=True,
                                                      tonkatsu_broker_peer_sha256="a" * 64)),
            scope={"scheme": "https", "state": {"foodtech_tonkatsu_peer_sha256": "b" * 64}},
            headers={"Content-Type": CONTENT_TYPE},
            stream=Mock(),
        )
        resolve = Mock()
        with self.assertRaises(HTTPException) as error:
            await receive_internal_tonkatsu_handoff(request, resolve_scope=resolve, expected_role="unused",
                                                    expected_role_id="unused")
        self.assertEqual(error.exception.status_code, 401)
        request.stream.assert_not_called()
        resolve.assert_not_called()
