import unittest
from dataclasses import replace
from uuid import uuid4

import boto3
from botocore.stub import ANY, Stubber
from foodtech_business_api.guarded_deletion import GuardedDeletion, VaultSnapshot


class Client:
    def __init__(self):
        self.requests = []

    def transact_write_items(self, **kwargs):
        self.requests.append(kwargs)


class GuardedDeletionTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.store = GuardedDeletion(auth_table="auth", registry_table="registry", vault_table="vault",
                                     capsule_table="capsule", approved_purposes=frozenset({"BILLING"}),
                                     client=self.client)
        self.subject, self.request_id = str(uuid4()), str(uuid4())
        self.snapshot = VaultSnapshot(self.subject, str(uuid4()), 4, "A" * 43, 8, 0)
        self.arguments = {"subject": self.subject, "request_id": self.request_id, "snapshot": self.snapshot,
                          "encrypted_user_id": b"encrypted-id", "encrypted_capsule_locator": b"encrypted-locator",
                          "requested_at": "2026-11-21T00:00:00Z", "policy_version": "v1"}

    def test_four_domains_are_locked_in_one_guarded_transaction(self):
        self.store.lock_and_register(**self.arguments)
        request = self.client.requests[0]
        self.assertEqual(request["ClientRequestToken"], self.request_id)
        self.assertEqual(len(request["TransactItems"]), 4)
        auth, vault, capsule, registry = request["TransactItems"]
        self.assertEqual(auth["Update"]["TableName"], "auth")
        self.assertEqual(vault["Update"]["TableName"], "vault")
        self.assertEqual(capsule["Update"]["TableName"], "capsule")
        self.assertEqual(registry["Put"]["TableName"], "registry")
        self.assertIn("capsule_locator = :locator", vault["Update"]["ConditionExpression"])
        self.assertEqual(vault["Update"]["ExpressionAttributeValues"][":expected"], {"N": "4"})
        self.assertEqual(capsule["Update"]["ExpressionAttributeValues"][":expected"], {"N": "8"})
        self.assertEqual(registry["Put"]["Item"]["encrypted_user_id"], {"B": b"encrypted-id"})
        self.assertNotIn("subject", registry["Put"]["Item"])

    def test_owner_mismatch_and_invalid_snapshot_fail_before_any_write(self):
        for snapshot in (replace(self.snapshot, authenticated_subject=str(uuid4())),
                         replace(self.snapshot, vault_revision=0),
                         replace(self.snapshot, capsule_locator="invalid"),
                         replace(self.snapshot, capsule_revision=True)):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                self.store.lock_and_register(**{**self.arguments, "snapshot": snapshot})
        self.assertEqual(self.client.requests, [])

    def test_tables_must_remain_separate(self):
        with self.assertRaises(ValueError):
            GuardedDeletion(auth_table="same", registry_table="registry", vault_table="same", capsule_table="capsule",
                            approved_purposes=frozenset({"BILLING"}), client=self.client)

    def test_botocore_accepts_four_item_transaction(self):
        client = boto3.client("dynamodb", region_name="ap-southeast-2", aws_access_key_id="synthetic",
                              aws_secret_access_key="synthetic")
        store = GuardedDeletion(auth_table="auth", registry_table="registry", vault_table="vault",
                                capsule_table="capsule", approved_purposes=frozenset({"BILLING"}), client=client)
        with Stubber(client) as stubber:
            stubber.add_response("transact_write_items", {}, {"ClientRequestToken": self.request_id, "TransactItems": ANY})
            store.lock_and_register(**self.arguments)
            stubber.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
