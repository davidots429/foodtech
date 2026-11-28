import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from foodtech_business_api.combi_customer_invocation import (
    CombiBrokerDeliveryUnavailable, _deliver_bff_delegation,
)
from foodtech_business_api.combi_delegation import TONKATSU_CONTENT_TYPE, TonkatsuInvocationDelegation
from foodtech_business_api.combi_read_delivery import CombiDeliverySettings
from foodtech_business_api.tonkatsu_broker_app import app
from foodtech_business_api.tonkatsu_read_broker import TonkatsuReadBroker

from test_combi_broker_app import listening, tls


class TonkatsuBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_pinned_bff_delegates_and_lease_never_leaves_broker(self):
        module = "foodtech_business_api.tonkatsu_broker_app"
        delegation = TonkatsuInvocationDelegation("customer", 2, str(uuid4()), int(time.time()) + 300)
        scope, lease = Mock(), Mock()
        broker = object.__new__(TonkatsuReadBroker)
        broker.issue = Mock(return_value=(scope, lease))
        broker.resolver = Mock()
        gateway = CombiDeliverySettings("https://gateway.internal", "server", "client", "key")
        runtime = Mock(broker=broker, gateway=gateway)
        outcome = {"execution_id": delegation.execution_id, "state": "DENIED", "response_text": None,
                   "error_code": "RECOMMENDATION_EVIDENCE_UNVERIFIED"}
        deliver = AsyncMock(return_value=outcome)
        with tempfile.TemporaryDirectory() as temporary:
            certs = await asyncio.to_thread(tls.certificates, Path(temporary))
            with (
                patch.dict(os.environ, {"FOODTECH_TONKATSU_BROKER_ENABLED": "true"}),
                patch(f"{module}.configured_tonkatsu_broker_runtime", return_value=runtime) as configure,
                patch("foodtech_business_api.tonkatsu_broker_invocation.deliver_internal_tonkatsu_handoff", deliver),
            ):
                async with listening(certs, broker_app=app, recipe="tonkatsu") as url:
                    async def send(peer):
                        settings = CombiDeliverySettings(url, certs["server"][0], *certs[peer])
                        return await _deliver_bff_delegation(delegation, settings, recipe="tonkatsu",
                            delegation_type=TonkatsuInvocationDelegation, content_type=TONKATSU_CONTENT_TYPE)

                    with self.assertRaises(CombiBrokerDeliveryUnavailable):
                        await send("other_business")
                    configure.assert_not_called()
                    self.assertEqual(await send("business"), outcome)
            configure.assert_called_once_with(delegation)
            broker.issue.assert_called_once_with(owner=delegation.subject, epoch=delegation.epoch,
                execution_id=delegation.execution_id,
                deadline=datetime.fromtimestamp(delegation.session_expires, timezone.utc))
            lease._internal_handoff.assert_called_once_with(scope)
            deliver.assert_awaited_once_with(lease._internal_handoff.return_value, scope, gateway)
            broker.resolver.history.revalidate_tonkatsu.assert_called_once_with(scope.history)
            lease.close.assert_called_once()
            runtime.close.assert_called_once()
        del app.state.tonkatsu_bff_leaf_sha256

    async def test_bff_forwards_authenticated_session_and_rechecks_epoch(self):
        from foodtech_business_api.main import app as customer_app

        identifier = str(uuid4())
        session = SimpleNamespace(surface="customer", subject="owner", epoch=2, csrf="test-csrf",
                                  expires=int(time.time()) + 300)
        changed = SimpleNamespace(**{**vars(session), "epoch": 3})
        lookup = AsyncMock(side_effect=[session, changed])
        reply = AsyncMock(return_value={"execution_id": identifier, "state": "FAILED",
                                       "response_text": None, "error_code": "LIMIT_CHECK_FAILED"})
        module = "foodtech_business_api.combi_customer_invocation"
        flags = {"FOODTECH_TONKATSU_CUSTOMER_INVOCATION_ENABLED": "true",
                 "FOODTECH_PRINCIPAL_PROVISIONING_ENABLED": "true"}
        with (
            patch.dict(os.environ, flags),
            patch(f"{module}._session", lookup),
            patch(f"{module}._csrf"),
            patch(f"{module}._require_principal_record", AsyncMock()),
            patch(f"{module}._broker_settings", return_value=Mock()),
            patch(f"{module}._deliver_bff_delegation", reply),
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=customer_app), base_url="http://local") as http:
                response = await http.post(f"/api/customer/ai/tonkatsu-recipe-executions/{identifier}/invoke")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(lookup.await_count, 2)
        reply.assert_awaited_once()
        self.assertEqual(reply.await_args.args[0], TonkatsuInvocationDelegation("owner", 2, identifier, session.expires))
