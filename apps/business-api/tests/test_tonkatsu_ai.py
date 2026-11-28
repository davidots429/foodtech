import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from foodtech_business_api.main import app


class TonkatsuRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_customer_uses_owned_versions_and_cannot_supply_account_or_source_data(self):
        path = "/api/customer/ai/tonkatsu-recipe-requests"
        body = {"request_id": str(uuid4()), "job_id": str(uuid4()), "job_version": 2,
                "prior_job_id": str(uuid4()), "prior_job_version": 3}
        session = SimpleNamespace(subject="authenticated-subject", epoch=7)
        current = {"version": 2, "menu": "TONKATSU", "provenance": "SYNTHETIC", "status": "COMPLETED",
                   "equipment_profile": "synthetic-fryer-approved-v2"}
        prior = {**current, "version": 3}
        response_id = str(uuid4())
        read = AsyncMock(side_effect=[(session, None, None, row, None) for row in (current, prior)])
        gateway = AsyncMock(return_value={"execution_id": response_id, "state": "PENDING"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as http:
            with patch("foodtech_business_api.cooking._read", read), patch("foodtech_business_api.ai._recipe_gateway", gateway):
                invalid = await http.post(path, json={**body, "tenant_id": "forged"})
                self.assertEqual(invalid.status_code, 422)
                read.assert_not_awaited()
                response = await http.post(path, json=body)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json(), {"execution_id": response_id, "state": "PENDING"})
                self.assertEqual([call.args[1] for call in read.await_args_list], [body["job_id"], body["prior_job_id"]])
                self.assertTrue(all(call.kwargs["write"] for call in read.await_args_list))
                gateway.assert_awaited_once_with("/internal/ai/tonkatsu-recipe-requests",
                    {**body, "tenant_id": session.subject, "auth_epoch": session.epoch})
                read.side_effect = [(session, None, None, row, None) for row in (current, {**prior, "version": 4})]
                gateway.reset_mock()
                stale = await http.post(path, json=body)
                self.assertEqual(stale.status_code, 409)
                gateway.assert_not_awaited()
