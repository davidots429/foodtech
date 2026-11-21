import unittest
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from foodtech_business_api.deletion_registry_store import DeletionRegistryConflict, DeletionRegistryStore


class TransactionClient:
    def __init__(self):
        self.items = {}
        self.requests = []
        self.fail = None

    def transact_write_items(self, *, TransactItems):
        self.requests.append(TransactItems)
        if self.fail:
            raise ClientError({"Error": {"Code": self.fail, "Message": "failure"}}, "TransactWriteItems")
        update, put = TransactItems[0]["Update"], TransactItems[1]["Put"]
        subject = update["Key"]["pk"]["S"]
        item = put["Item"]
        self.items[(update["TableName"], subject, "")] = {
            "pk": {"S": subject}, "suspended": {"BOOL": True},
            "deletion_lock_request": update["ExpressionAttributeValues"][":request"]}
        self.items[(put["TableName"], item["pk"]["S"], item["sk"]["S"])] = item

    def get_item(self, *, TableName, Key, ConsistentRead, **kwargs):
        assert ConsistentRead is True
        item = self.items.get((TableName, Key["pk"]["S"], Key.get("sk", {}).get("S", "")))
        return {"Item": item} if item else {}


class DeletionRegistryStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = TransactionClient()
        self.store = DeletionRegistryStore("registry", "auth", client=self.client)
        self.request = {"subject": str(uuid4()), "request_id": str(uuid4()),
                        "encrypted_user_id": b"encrypted-user", "encrypted_capsule_locator": b"encrypted-capsule",
                        "requested_at": "2026-11-21T00:00:00Z", "policy_version": "v1"}

    def test_lock_and_encrypted_tombstone_are_one_conditional_transaction(self):
        self.assertTrue(self.store.lock_and_register(**self.request))
        transaction = self.client.requests[0]
        update, put = transaction[0]["Update"], transaction[1]["Put"]
        self.assertIn("attribute_not_exists(deletion_lock_request)", update["ConditionExpression"])
        self.assertIn("suspended = :yes", update["UpdateExpression"])
        self.assertIn("ADD #epoch :one", update["UpdateExpression"])
        self.assertEqual(put["Item"]["pk"], {"S": "REGISTRY"})
        self.assertEqual(put["Item"]["status"], {"S": "DELETING"})
        self.assertEqual(put["Item"]["encrypted_user_id"], {"B": b"encrypted-user"})
        self.assertNotIn("pseudonym_digest", put["Item"])
        self.assertNotIn("subject", put["Item"])

    def test_exact_retry_is_idempotent_but_different_request_or_ciphertext_conflicts(self):
        self.store.lock_and_register(**self.request)
        self.client.fail = "TransactionCanceledException"
        self.assertFalse(self.store.lock_and_register(**self.request))
        with self.assertRaises(DeletionRegistryConflict):
            self.store.lock_and_register(**{**self.request, "encrypted_user_id": b"different"})
        with self.assertRaises(DeletionRegistryConflict):
            self.store.lock_and_register(**{**self.request, "subject": str(uuid4())})

    def test_aws_errors_are_not_misreported_as_success(self):
        self.client.fail = "AccessDeniedException"
        with self.assertRaises(ClientError):
            self.store.lock_and_register(**self.request)
        self.client.fail = "TransactionCanceledException"
        with self.assertRaises(DeletionRegistryConflict):
            self.store.lock_and_register(**self.request)

    def test_invalid_or_unencrypted_type_is_rejected_before_write(self):
        for change in ({"subject": "not-uuid"}, {"requested_at": "2026-11-21T00:00:00"},
                       {"encrypted_user_id": "plaintext"}, {"policy_version": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.lock_and_register(**{**self.request, **change})
        self.assertEqual(self.client.requests, [])

    def test_real_botocore_client_accepts_transaction_wire_shape(self):
        client = boto3.client("dynamodb", region_name="ap-southeast-2", aws_access_key_id="synthetic",
                              aws_secret_access_key="synthetic")
        with Stubber(client) as stubber:
            stubber.add_response("transact_write_items", {}, {"TransactItems": ANY})
            store = DeletionRegistryStore("registry", "auth", client=client)
            self.assertTrue(store.lock_and_register(**self.request))
            stubber.assert_no_pending_responses()

    def test_recovery_after_lost_response_returns_only_nonsecret_request_metadata(self):
        self.assertIsNone(self.store.locked_request(self.request["subject"]))
        self.store.lock_and_register(**self.request)
        recovered = self.store.locked_request(self.request["subject"])
        self.assertEqual(recovered, {"request_id": self.request["request_id"], "status": "DELETING", "version": 1,
                                     "requested_at": self.request["requested_at"], "policy_version": "v1"})
        self.assertNotIn("encrypted", str(recovered))

    def test_missing_tombstone_or_inconsistent_lock_is_not_treated_as_success(self):
        self.store.lock_and_register(**self.request)
        user_key = ("auth", f'USER#{self.request["subject"]}', "")
        registry_key = ("registry", "REGISTRY", f'REQ#{self.request["request_id"]}')
        saved = self.client.items.pop(registry_key)
        with self.assertRaises(DeletionRegistryConflict):
            self.store.locked_request(self.request["subject"])
        self.client.items[registry_key] = saved
        self.client.items[user_key]["suspended"] = {"BOOL": False}
        with self.assertRaises(DeletionRegistryConflict):
            self.store.locked_request(self.request["subject"])

    def test_changed_lock_during_recovery_is_rejected(self):
        self.store.lock_and_register(**self.request)
        original = self.client.get_item
        seen = []
        def changed(**kwargs):
            response = original(**kwargs)
            if kwargs["TableName"] == "auth":
                seen.append(1)
                if len(seen) == 2:
                    return {"Item": {**response["Item"], "epoch": {"N": "99"}}}
            return response
        self.client.get_item = changed
        with self.assertRaisesRegex(DeletionRegistryConflict, "changed during lookup"):
            self.store.locked_request(self.request["subject"])


if __name__ == "__main__":
    unittest.main()
