import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from boto3.dynamodb.types import TypeSerializer
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_business_api.cooking_store import DynamoCookingStore, MemoryCookingStore
from foodtech_business_api.main import app


class ValidationSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subject = "owner"
        self.store = MemoryCookingStore()
        self.job_id = str(uuid4())
        self.pk = cooking._pk(self.subject, self.job_id)
        self.store.items[(self.pk, "META")] = {"job_id": self.job_id, "owner_subject": self.subject,
            "menu": "JEYUK", "version": 3, "source_binding": {"batch_id": "jeyuk-test"}}
        self.receipt = {"pk": "JEYUK#jeyuk-test", "sk": "COLLECTION#RECEIPT#test",
                        "record_count": 2, "ordered_raw_sha256": "a" * 64}
        self.store.validation_sources[("foodtech-data-flow-dev", self.receipt["pk"])] = {
            "revision": 2, "receipt": self.receipt.copy()}
        self.evidence = {"status": "LINKED_UNVERIFIED", "job_version": 3, "context_digest": "digest",
            "context_version": 1, "safety_version": 2, "discharge_check_version": 1,
            "source_revision": 2, "source_receipt_reference": self.receipt.copy(), "checks": {"test": True}}
        async def session(_request, surface):
            return Session(surface, self.subject, None, "csrf", 9999999999, 0)
        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false", "FOODTECH_DATA_TABLE": "foodtech-data-flow-dev"}),
            patch.object(cooking, "_session", side_effect=session),
            patch.object(cooking, "get_cooking_store", return_value=self.store),
            patch.object(cooking, "jeyuk_validation_evidence", new=AsyncMock(side_effect=lambda *_: self.evidence))]
        for item in self.patches: item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.path = f"/api/customer/cooking-jobs/{self.job_id}/validation-evidence-snapshots"

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches): item.stop()

    async def test_capture_is_immutable_unverified_and_scoped(self):
        response = await self.client.post(self.path, headers={"X-CSRF-Token": "csrf"}, json={"expected_version": 3})
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()
        self.assertEqual(item["status"], "LINKED_UNVERIFIED")
        self.assertFalse(item["activation_allowed"])
        self.assertEqual(self.store.items[(self.pk, item["sk"])]["source_receipt_reference"], self.receipt)
        listed = await self.client.get(self.path)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([entry["snapshot_id"] for entry in listed.json()], [item["snapshot_id"]])
        self.assertEqual(listed.headers["cache-control"], "no-store")
        self.subject = "other"
        self.assertEqual((await self.client.post(self.path, headers={"X-CSRF-Token": "csrf"}, json={"expected_version": 3})).status_code, 404)
        self.assertEqual((await self.client.get(self.path)).status_code, 404)

    async def test_changed_job_receipt_or_revision_fails_closed(self):
        source = self.store.validation_sources[("foodtech-data-flow-dev", self.receipt["pk"])]
        for field, value in (("revision", 3), ("receipt", {**self.receipt, "ordered_raw_sha256": "b" * 64})):
            old = source[field]; source[field] = value
            response = await self.client.post(self.path, headers={"X-CSRF-Token": "csrf"}, json={"expected_version": 3})
            self.assertEqual(response.status_code, 409)
            source[field] = old
        self.store.items[(self.pk, "META")]["version"] = 4
        self.assertEqual((await self.client.post(self.path, headers={"X-CSRF-Token": "csrf"}, json={"expected_version": 3})).status_code, 409)
        self.assertFalse(any(sk.startswith("VALIDATION#EVIDENCE#") for _pk, sk in self.store.items))

    async def test_incomplete_observation_cannot_be_saved(self):
        self.evidence["status"] = "INCOMPLETE"
        response = await self.client.post(self.path, headers={"X-CSRF-Token": "csrf"}, json={"expected_version": 3})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any(sk.startswith("VALIDATION#EVIDENCE#") for _pk, sk in self.store.items))

class DynamoSnapshotShapeTests(unittest.TestCase):
    def test_job_revision_and_exact_receipt_are_in_same_transaction(self):
        calls = []
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="foodtech-cooking-jobs-dev")
        store.serializer = TypeSerializer()
        store.client = SimpleNamespace(transact_write_items=lambda **kwargs: calls.append(kwargs))
        receipt = {"pk": "JEYUK#batch", "sk": "COLLECTION#RECEIPT#test",
                   "record_count": 2, "ordered_raw_sha256": "a" * 64}
        snapshot = {"sk": f"VALIDATION#EVIDENCE#{uuid4()}", "status": "LINKED_UNVERIFIED",
                    "job_version": 3, "source_revision": 2, "source_receipt_reference": receipt}
        store.save_validation_evidence("SCOPE#USER#owner#JOB#job", 3, snapshot,
                                       "foodtech-data-flow-dev", 2, receipt)
        operations = calls[0]["TransactItems"]
        shape = get_session().get_service_model("dynamodb").operation_model("TransactWriteItems").input_shape
        validate_parameters(calls[0], shape)
        self.assertEqual(len(operations), 4)
        self.assertEqual([list(item)[0] for item in operations], ["ConditionCheck"] * 3 + ["Put"])
        self.assertEqual(operations[2]["ConditionCheck"]["ExpressionAttributeValues"][":hash"], {"S": "a" * 64})


if __name__ == "__main__": unittest.main()
