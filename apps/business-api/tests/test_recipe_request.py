import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from foodtech_business_api import ai, cooking
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class RecipeRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.job = str(uuid4())
        self.subject = "owner"
        self.store = MemoryCookingStore()
        self.pk = cooking._pk(self.subject, self.job)
        self.store.items[(self.pk, "META")] = {"job_id": self.job, "version": 7}
        async def session(request, surface):
            return SimpleNamespace(subject=self.subject, csrf="token")
        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}),
                        patch.object(cooking, "_session", side_effect=session),
                        patch.object(cooking, "get_cooking_store", return_value=self.store),
                        patch("foodtech_business_api.recommendation_evidence.read_evidence", new_callable=AsyncMock,
                              return_value={"job_version": 7, "completeness": "NOT_ASSESSED",
                                            "recorded": {"provenance": "SYNTHETIC", "menu": "TONKATSU"}}),
                        patch.object(ai, "_recipe_gateway", new_callable=AsyncMock,
                                     return_value={"execution_id": str(uuid4()), "state": "BLOCKED"})]
        for item in self.patches: item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.body = {"job_id": self.job, "job_version": 7}
        self.headers = {"X-CSRF-Token": "token"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches): item.stop()

    async def test_owned_request_forwards_authoritative_tenant_and_binding(self):
        response = await self.client.post("/api/customer/ai/recipe-requests", headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 201)
        ai._recipe_gateway.assert_awaited_once_with("/internal/ai/recipe-requests", {
            "tenant_id": "owner", "job_id": self.job, "job_version": 7})
        self.subject = "another"
        self.assertEqual((await self.client.post("/api/customer/ai/recipe-requests", headers=self.headers,
                                                  json=self.body)).status_code, 404)

    async def test_csrf_and_stale_or_insufficient_evidence_are_blocked(self):
        path = "/api/customer/ai/recipe-requests"
        self.assertEqual((await self.client.post(path, json=self.body)).status_code, 403)
        self.store.items[(self.pk, "META")]["version"] = 8
        self.assertEqual((await self.client.post(path, headers=self.headers, json=self.body)).status_code, 409)
        self.store.items[(self.pk, "META")]["version"] = 7
        from foodtech_business_api.recommendation_evidence import read_evidence
        read_evidence.return_value["completeness"] = "INSUFFICIENT"
        self.assertEqual((await self.client.post(path, headers=self.headers, json=self.body)).status_code, 409)
        read_evidence.return_value["completeness"] = "NOT_ASSESSED"
        read_evidence.return_value["source_revision_status"] = "CHANGED"
        self.assertEqual((await self.client.post(path, headers=self.headers, json=self.body)).status_code, 409)
        ai._recipe_gateway.assert_not_awaited()

    async def test_selected_comparison_is_revalidated_for_each_step(self):
        from foodtech_business_api.recommendation_evidence import read_evidence
        comparison = "a" * 64
        body = {**self.body, "comparison_id": comparison}
        parent, quote = str(uuid4()), str(uuid4())
        paths = [("/api/customer/ai/recipe-requests", body),
                 (f"/api/customer/ai/recipe-requests/{parent}/local-preview", body),
                 (f"/api/customer/ai/recipe-quotes/{quote}/confirm", {**body,
                     "notice_version": "local-recipe-downgrade-v1", "quality_warning_acknowledged": True})]
        for path, payload in paths:
            with self.subTest(path=path):
                response = await self.client.post(path, headers=self.headers, json=payload)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(read_evidence.await_args.kwargs["comparison_id"], comparison)
                self.assertEqual(ai._recipe_gateway.await_args.args[1]["comparison_id"], comparison)
        read_evidence.side_effect = __import__("fastapi").HTTPException(409, "비교가 변경되었습니다")
        ai._recipe_gateway.reset_mock()
        for path, payload in paths:
            self.assertEqual((await self.client.post(path, headers=self.headers, json=payload)).status_code, 409)
        ai._recipe_gateway.assert_not_awaited()
        self.assertEqual((await self.client.post(paths[0][0], headers=self.headers,
                              json={**body, "comparison_id": "invalid"})).status_code, 422)

    async def test_preview_and_confirm_forward_binding_and_strict_acknowledgement(self):
        parent, quote = str(uuid4()), str(uuid4())
        preview = await self.client.post(f"/api/customer/ai/recipe-requests/{parent}/local-preview",
                                         headers=self.headers, json=self.body)
        self.assertEqual(preview.status_code, 201)
        self.assertEqual(ai._recipe_gateway.await_args.args[1]["job_id"], self.job)
        payload = {**self.body, "notice_version": "local-recipe-downgrade-v1",
                   "quality_warning_acknowledged": "true"}
        path = f"/api/customer/ai/recipe-quotes/{quote}/confirm"
        self.assertEqual((await self.client.post(path, headers=self.headers, json=payload)).status_code, 422)
        payload["quality_warning_acknowledged"] = True
        confirmed = await self.client.post(path, headers=self.headers, json=payload)
        self.assertEqual(confirmed.status_code, 201)
        self.assertFalse(confirmed.json()["execution_available"])
        self.assertEqual(ai._recipe_gateway.await_args.args[1]["job_version"], 7)
