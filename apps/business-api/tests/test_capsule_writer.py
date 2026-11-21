import unittest
from unittest.mock import patch
from uuid import uuid4

import boto3
from botocore.stub import ANY, Stubber
from foodtech_business_api.capsule_writer import CapsuleWriter


LOCATOR = "A" * 43
KEY_METADATA = {"pool_version": "pool-v1", "shard_id": "shard-0",
                "key_arn": "arn:aws:kms:ap-southeast-2:123456789012:key/test-key"}


class Client:
    def __init__(self):
        self.puts = []
        self.transactions = []

    def put_item(self, **kwargs):
        self.puts.append(kwargs)

    def transact_write_items(self, **kwargs):
        self.transactions.append(kwargs["TransactItems"])


class CapsuleWriterTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.writer = CapsuleWriter("capsule-table", frozenset({"BILLING", "USAGE"}), client=self.client)

    def test_creation_uses_random_locator_and_does_not_overwrite(self):
        with patch("foodtech_business_api.capsule_writer.secrets.token_urlsafe", return_value=LOCATOR) as random:
            locator, action = self.writer.create_action()
            self.assertEqual(locator, LOCATOR)
        random.assert_called_once_with(32)
        request = action["Put"]
        self.assertEqual(request["Item"]["state"], {"S": "ACTIVE"})
        self.assertEqual(request["Item"]["revision"], {"N": "1"})
        self.assertIn("attribute_not_exists", request["ConditionExpression"])
        self.assertEqual(self.client.puts, [])
        self.assertEqual(self.client.transactions, [])

    def test_entry_write_and_revision_are_one_conditioned_transaction(self):
        self.writer.put_entry(locator=LOCATOR, purpose="BILLING", epoch="2026-11",
                              ciphertext=b"encrypted", expected_revision=4, **KEY_METADATA)
        transaction = self.client.transactions[0]
        update, put = transaction[0]["Update"], transaction[1]["Put"]
        self.assertEqual(update["ExpressionAttributeValues"][":expected"], {"N": "4"})
        self.assertEqual(update["ExpressionAttributeValues"][":next"], {"N": "5"})
        self.assertIn("#state = :active", update["ConditionExpression"])
        self.assertEqual(put["Item"]["sk"], {"S": "ENTRY#BILLING#2026-11"})
        self.assertEqual(put["Item"]["ciphertext"], {"B": b"encrypted"})
        self.assertEqual(put["Item"]["pool_version"], {"S": "pool-v1"})
        self.assertIn("attribute_not_exists", put["ConditionExpression"])

    def test_lock_is_only_a_transaction_action_and_checks_state_and_revision(self):
        request_id = str(uuid4())
        action = self.writer.lock_action(locator=LOCATOR, request_id=request_id, expected_revision=4)["Update"]
        self.assertEqual(action["ExpressionAttributeValues"][":locked"], {"S": "DELETION_LOCKED"})
        self.assertEqual(action["ExpressionAttributeValues"][":request"], {"S": request_id})
        self.assertIn("#revision = :expected", action["ConditionExpression"])
        self.assertEqual(self.client.transactions, [])

    def test_invalid_input_cannot_issue_write(self):
        base = {"locator": LOCATOR, "purpose": "BILLING", "epoch": "2026-11",
                "ciphertext": b"encrypted", "expected_revision": 1, **KEY_METADATA}
        for invalid in ({"locator": "email@example.com"}, {"purpose": "UNAPPROVED"},
                        {"epoch": "0000-01"}, {"epoch": "2026-13"}, {"ciphertext": b""},
                        {"ciphertext": b"x" * 400_001}, {"expected_revision": True}, {"key_arn": "not-a-key"}):
            with self.subTest(invalid=tuple(invalid)), self.assertRaises(ValueError):
                self.writer.put_entry(**{**base, **invalid})
        with self.assertRaises(ValueError):
            self.writer.lock_action(locator=LOCATOR, request_id="bad", expected_revision=1)
        self.assertEqual(self.client.transactions, [])

    def test_real_botocore_client_accepts_transaction_shape(self):
        client = boto3.client("dynamodb", region_name="ap-southeast-2", aws_access_key_id="synthetic",
                              aws_secret_access_key="synthetic")
        with Stubber(client) as stubber:
            stubber.add_response("transact_write_items", {}, {"TransactItems": ANY})
            CapsuleWriter("capsule-table", frozenset({"BILLING"}), client=client).put_entry(
                locator=LOCATOR, purpose="BILLING", epoch="2026-11", ciphertext=b"encrypted", expected_revision=1,
                **KEY_METADATA)
            stubber.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
