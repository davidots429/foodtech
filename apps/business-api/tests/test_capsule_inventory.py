import unittest

import boto3
from botocore.stub import Stubber
from foodtech_business_api.capsule_inventory import CapsuleInventory, CapsuleInventoryError


LOCATOR = "A" * 43
REQUEST_ID = "12c5f2d4-c6d2-4165-adf7-e7fed537af06"


def entry(sk, ciphertext=b"ciphertext"):
    return {"pk": {"S": LOCATOR}, "sk": {"S": sk}, "ciphertext": {"B": ciphertext},
            "pool_version": {"S": "pool-v1"}, "shard_id": {"S": "shard-0"},
            "key_arn": {"S": "arn:aws:kms:ap-southeast-2:123456789012:key/test-key"},
            "user_key_id": {"S": "d9b699c2-61c7-462e-a65d-15f019a0cf13"}}


def meta(revision="1", state="DELETION_LOCKED"):
    return {"pk": {"S": LOCATOR}, "sk": {"S": "META"}, "revision": {"N": revision}, "state": {"S": state},
            "deletion_request_id": {"S": REQUEST_ID}, "user_key_id": {"S": "d9b699c2-61c7-462e-a65d-15f019a0cf13"},
            "encrypted_user_dek": {"B": b"wrapped"}, "pool_version": {"S": "pool-v1"}, "shard_id": {"S": "shard-0"},
            "key_arn": {"S": "arn:aws:kms:ap-southeast-2:123456789012:key/test-key"},
            "scope_purpose": {"S": "credential"}, "scope_epoch": {"S": "2026-11"}, "scope_key_version": {"S": "v1"}}


class Paginator:
    def __init__(self, pages):
        self.pages = pages
        self.request = None

    def paginate(self, **kwargs):
        self.request = kwargs
        yield from self.pages


class Client:
    def __init__(self, pages, metadata=None):
        self.paginator = Paginator(pages)
        self.metadata = metadata or [meta(), meta()]
        self.gets = []

    def get_item(self, **kwargs):
        self.gets.append(kwargs)
        return {"Item": self.metadata.pop(0)}

    def get_paginator(self, name):
        assert name == "query"
        return self.paginator


class CapsuleInventoryTests(unittest.TestCase):
    def inventory(self, client):
        return CapsuleInventory("capsule-table", frozenset({"BILLING", "USAGE"}), client=client)

    def test_stages_all_pages_and_checks_locked_metadata_before_and_after(self):
        client = Client([{"Items": [entry("ENTRY#BILLING#2026-01")]},
                         {"Items": [entry("ENTRY#USAGE#2026-11", b"other-ciphertext")]}])
        staged = []
        result = self.inventory(client).stage(LOCATOR, REQUEST_ID, lambda sk, blob, metadata: staged.append((sk, blob, metadata)))
        self.assertEqual((result.revision, result.entry_count), (1, 2))
        self.assertEqual(result.wrapped_user_key.encrypted_dek, b"wrapped")
        self.assertEqual(result.wrapped_user_key.user_key_id, staged[0][2]["user_key_id"])
        self.assertNotIn("wrapped", repr(result))
        self.assertNotIn("wrapped", repr(result.wrapped_user_key))
        self.assertEqual([row[0] for row in staged], ["ENTRY#BILLING#2026-01", "ENTRY#USAGE#2026-11"])
        self.assertEqual(staged[0][2]["pool_version"], "pool-v1")
        self.assertTrue(client.paginator.request["ConsistentRead"])
        self.assertTrue(all(request["ConsistentRead"] for request in client.gets))
        self.assertEqual(len(client.gets), 2)

    def test_changed_or_unlocked_capsule_is_not_accepted(self):
        changed_key = {**meta(), "encrypted_user_dek": {"B": b"other-wrapped"}}
        for metadata in ([meta("1"), meta("2")], [meta("1", "ACTIVE")], [meta(), changed_key]):
            with self.subTest(metadata=metadata):
                client = Client([{"Items": [entry("ENTRY#BILLING#2026-01")]}], metadata)
                with self.assertRaises(CapsuleInventoryError):
                    self.inventory(client).stage(LOCATOR, REQUEST_ID, lambda *_: None)
        with self.assertRaises(CapsuleInventoryError):
            self.inventory(Client([{"Items": []}])).stage(
                LOCATOR, "5b82f17f-f864-4575-b106-516a7c865993", lambda *_: None)

    def test_malformed_duplicate_unknown_and_cross_capsule_entries_fail(self):
        invalid = [entry("ENTRY#OTHER#2026-01"), entry("ENTRY#BILLING#2026-13"),
                   entry("ENTRY#BILLING#0000-01"), entry("ENTRY#BILLING#2026-01", b""),
                   {**entry("ENTRY#BILLING#2026-01"), "key_arn": {"S": "not-a-key"}},
                   {**entry("ENTRY#BILLING#2026-01"), "user_key_id": {"S": "ba246043-0dd0-49b0-b5e6-0615c8b0bfb3"}},
                   {**entry("ENTRY#BILLING#2026-01"), "pk": {"S": "B" * 43}}]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(CapsuleInventoryError):
                self.inventory(Client([{"Items": [item]}])).stage(LOCATOR, REQUEST_ID, lambda *_: None)
        duplicate = entry("ENTRY#BILLING#2026-01")
        with self.assertRaises(CapsuleInventoryError):
            self.inventory(Client([{"Items": [duplicate]}, {"Items": [duplicate]}])).stage(LOCATOR, REQUEST_ID, lambda *_: None)

    def test_empty_capsule_and_more_than_one_thousand_entries_are_not_truncated(self):
        empty = self.inventory(Client([{"Items": []}])).stage(LOCATOR, REQUEST_ID, lambda *_: None)
        self.assertEqual(empty.entry_count, 0)
        entries = [entry(f"ENTRY#USAGE#{year:04d}-{month:02d}")
                   for year in range(1900, 1990) for month in range(1, 13)]
        count = self.inventory(Client([{"Items": entries[:500]}, {"Items": entries[500:]}])).stage(
            LOCATOR, REQUEST_ID, lambda *_: None).entry_count
        self.assertEqual(count, 1080)

    def test_unkeyed_empty_capsule_is_allowed_but_partial_or_mismatched_key_is_not(self):
        unkeyed = {name: value for name, value in meta().items() if name not in {
            "user_key_id", "encrypted_user_dek", "pool_version", "shard_id", "key_arn",
            "scope_purpose", "scope_epoch", "scope_key_version"}}
        empty = self.inventory(Client([{"Items": []}], [unkeyed, unkeyed])).stage(
            LOCATOR, REQUEST_ID, lambda *_: None)
        self.assertEqual(empty.entry_count, 0)
        self.assertIsNone(empty.wrapped_user_key)
        with self.assertRaises(CapsuleInventoryError):
            self.inventory(Client([{"Items": [entry("ENTRY#BILLING#2026-01")]}], [unkeyed])).stage(
                LOCATOR, REQUEST_ID, lambda *_: None)
        partial = {**unkeyed, "user_key_id": meta()["user_key_id"]}
        with self.assertRaises(CapsuleInventoryError):
            self.inventory(Client([{"Items": []}], [partial])).stage(LOCATOR, REQUEST_ID, lambda *_: None)

    def test_scope_metadata_is_required_valid_and_stable(self):
        for changed in ({'scope_epoch': {'S': '2026-13'}}, {'scope_purpose': {'S': 'BAD'}},
                        {'scope_key_version': {'S': ''}}):
            with self.subTest(changed=changed), self.assertRaises(CapsuleInventoryError):
                self.inventory(Client([{'Items': []}], [{**meta(), **changed}])).stage(LOCATOR, REQUEST_ID, lambda *_: None)
        without_scope = {name: value for name, value in meta().items() if name != 'scope_epoch'}
        with self.assertRaises(CapsuleInventoryError):
            self.inventory(Client([{'Items': []}], [without_scope])).stage(LOCATOR, REQUEST_ID, lambda *_: None)
        with self.assertRaises(CapsuleInventoryError):
            self.inventory(Client([{'Items': []}], [meta(), {**meta(), 'scope_epoch': {'S': '2026-12'}}])).stage(
                LOCATOR, REQUEST_ID, lambda *_: None)

    def test_stage_failure_cannot_return_a_complete_inventory(self):
        client = Client([{"Items": [entry("ENTRY#BILLING#2026-01")]}])
        def fail(*_):
            raise RuntimeError("staging failed")
        with self.assertRaisesRegex(RuntimeError, "staging failed"):
            self.inventory(client).stage(LOCATOR, REQUEST_ID, fail)
        self.assertEqual(len(client.gets), 1)
        for pages in ([], [{}]):
            with self.subTest(pages=pages), self.assertRaises(CapsuleInventoryError):
                self.inventory(Client(pages)).stage(LOCATOR, REQUEST_ID, lambda *_: None)

    def test_real_botocore_paginator_reads_both_pages(self):
        client = boto3.client("dynamodb", region_name="ap-southeast-2", aws_access_key_id="synthetic",
                              aws_secret_access_key="synthetic")
        page_key = {"pk": {"S": LOCATOR}, "sk": {"S": "ENTRY#BILLING#2026-01"}}
        with Stubber(client) as stubber:
            stubber.add_response("get_item", {"Item": meta()})
            stubber.add_response("query", {"Items": [entry("ENTRY#BILLING#2026-01")],
                                            "LastEvaluatedKey": page_key})
            stubber.add_response("query", {"Items": [entry("ENTRY#USAGE#2026-11")]})
            stubber.add_response("get_item", {"Item": meta()})
            result = self.inventory(client).stage(LOCATOR, REQUEST_ID, lambda *_: None)
            self.assertEqual(result.entry_count, 2)
            stubber.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
