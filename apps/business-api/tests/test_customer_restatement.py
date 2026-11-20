import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from foodtech_business_api import cooking
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class CustomerRestatementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subject, self.job_id, self.request_id = "owner", str(uuid4()), str(uuid4())
        self.store = MemoryCookingStore()
        self.pk = cooking._pk(self.subject, self.job_id)
        self.store.items[(self.pk, "META")] = {"job_id": self.job_id, "version": 7, "status": "COMPLETED",
            "menu": "TONKATSU", "provenance": "SYNTHETIC", "source_binding": {
                "source_id": "source", "device_id": "device", "batch_id": "batch"}}
        async def session(_request, _surface): return SimpleNamespace(subject=self.subject, csrf="csrf")
        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false", "FOODTECH_DATA_TABLE": "data",
                                               "FOODTECH_COOKING_TABLE": "test-cooking",
                                               "FOODTECH_ORDERED_QUEUE_URL": "queue"}),
                        patch.object(cooking, "_session", side_effect=session),
                        patch.object(cooking, "get_cooking_store", return_value=self.store),
                        patch("foodtech_business_api.restatement_queue.source_revision", return_value=4),
                        patch("foodtech_business_api.restatement_queue.send")]
        for item in self.patches: item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.path = f"/api/customer/cooking-jobs/{self.job_id}/process-restatements"
        self.body = {"expected_version": 7, "request_id": self.request_id}
        self.headers = {"X-CSRF-Token": "csrf"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches): item.stop()

    async def test_request_is_owned_idempotent_and_retries_delivery(self):
        from foodtech_business_api import restatement_queue
        restatement_queue.send.side_effect = [OSError("synthetic"), None]
        self.assertEqual((await self.client.post(self.path, headers=self.headers, json=self.body)).status_code, 503)
        saved = self.store.customer_restatement(self.pk, self.request_id)
        self.assertEqual((saved["status"], saved["source_revision_at_request"]), ("PENDING", 4))
        result = await self.client.post(self.path, headers=self.headers, json=self.body)
        self.assertEqual((result.status_code, result.json()["status"]), (202, "PENDING"))
        self.assertEqual(restatement_queue.send.call_count, 2)
        status = await self.client.get(self.path + "/" + self.request_id)
        self.assertEqual(status.headers["cache-control"], "no-store")
        self.subject = "other"
        self.assertEqual((await self.client.get(self.path + "/" + self.request_id)).status_code, 404)

    async def test_rejects_stale_unbound_and_missing_csrf(self):
        self.assertEqual((await self.client.post(self.path, json=self.body)).status_code, 403)
        with patch.dict(os.environ, {"FOODTECH_COOKING_TABLE": ""}):
            self.assertEqual((await self.client.post(self.path, headers=self.headers, json=self.body)).status_code, 503)
        self.assertEqual((await self.client.post(self.path, headers=self.headers,
                          json={**self.body, "expected_version": 6})).status_code, 409)
        self.store.items[(self.pk, "META")]["source_binding"] = None
        self.assertEqual((await self.client.post(self.path, headers=self.headers, json=self.body)).status_code, 409)
