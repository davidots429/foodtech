import unittest
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

import boto3
from botocore.stub import Stubber

from foodtech_business_api.recovery_registry_inventory import (
    RecoveryRegistryInventory,
    RecoveryRegistryInventoryError,
)
from foodtech_contracts.recovery_reconciliation import Tombstone


ARN = "arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-deletion-registry-dev"


def item(number, status="DELETING", version=1):
    request = str(UUID(int=number))
    return {"pk": "REGISTRY", "sk": f"REQ#{request}", "request_id": request,
            "status": status, "version": Decimal(version)}


class RecoveryRegistryInventoryTests(unittest.TestCase):
    def setUp(self):
        self.dynamodb = Mock()
        self.table = self.dynamodb.Table.return_value
        self.inventory = RecoveryRegistryInventory(self.dynamodb, live_registry_arn=ARN)

    def test_sdk_request_reads_all_pages_and_returns_only_versioned_tombstones(self):
        dynamodb = boto3.resource("dynamodb", region_name="ap-southeast-2",
                                 aws_access_key_id="test", aws_secret_access_key="test")
        parameters = {
            "TableName": ARN,
            "ConsistentRead": True,
            "KeyConditionExpression": "pk = :registry AND begins_with(sk, :prefix)",
            "ProjectionExpression": "pk, sk, request_id, #status, #version",
            "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
            "ExpressionAttributeValues": {":registry": "REGISTRY", ":prefix": "REQ#"},
        }
        states = ("DELETING", "COMPLETED", "RECONCILING", "RECONCILE_FAILED")
        rows = [item(i, state, i) for i, state in enumerate(states, 1)]
        cursor = {key: rows[1][key] for key in ("pk", "sk")}
        last_cursor = {key: rows[-1][key] for key in ("pk", "sk")}

        def wire(row):
            return {name: {"N": str(value)} if isinstance(value, Decimal) else {"S": value}
                    for name, value in row.items()}

        with Stubber(dynamodb.meta.client) as stubber:
            stubber.add_response("query", {"Count": 2, "Items": list(map(wire, rows[:2])),
                                           "LastEvaluatedKey": wire(cursor)}, parameters)
            stubber.add_response("query", {"Count": 2, "Items": list(map(wire, rows[2:])),
                                           "LastEvaluatedKey": wire(last_cursor)},
                                 {**parameters, "ExclusiveStartKey": cursor})
            stubber.add_response("query", {"Count": 0, "Items": []},
                                 {**parameters, "ExclusiveStartKey": last_cursor})
            evidence = RecoveryRegistryInventory(dynamodb, live_registry_arn=ARN).read()
            stubber.assert_no_pending_responses()
        self.assertEqual([entry.tombstone for entry in evidence],
                         [Tombstone(row["request_id"], row["status"]) for row in rows])
        self.assertEqual([entry.version for entry in evidence], [1, 2, 3, 4])

    def test_empty_terminal_page_is_valid(self):
        self.table.query.return_value = {"Items": [], "Count": 0}
        self.assertEqual(self.inventory.read(), ())
        self.dynamodb.Table.assert_called_once_with(ARN)

    def test_invalid_entries_fail_closed(self):
        for fields in ({"request_id": str(UUID(int=2))}, {"status": "UNKNOWN"},
                       {"version": True}, {"version": Decimal("1.5")}, {"sk": "REQ#oops"}):
            with self.subTest(fields=fields):
                self.table.query.return_value = {"Items": [{**item(1), **fields}], "Count": 1}
                with self.assertRaises(RecoveryRegistryInventoryError):
                    self.inventory.read()

    def test_incomplete_duplicate_or_misordered_pages_fail_closed(self):
        first = item(1)
        cursor = {key: first[key] for key in ("pk", "sk")}
        page = {"Items": [first], "Count": 1, "LastEvaluatedKey": cursor}
        for invalid in ({"Count": 1}, {"Items": [], "Count": 1},
                        {"Items": [first], "Count": 1},
                        {"Items": [item(2)], "Count": 1, "LastEvaluatedKey": cursor}):
            with self.subTest(invalid=invalid):
                self.table.query.side_effect = [page, invalid]
                with self.assertRaises(RecoveryRegistryInventoryError):
                    self.inventory.read()

    def test_failed_followup_query_does_not_return_partial_results(self):
        row = item(1)
        self.table.query.side_effect = [
            {"Items": [row], "Count": 1, "LastEvaluatedKey": {key: row[key] for key in ("pk", "sk")}},
            RuntimeError("query unavailable"),
        ]
        with self.assertRaisesRegex(RuntimeError, "query unavailable"):
            self.inventory.read()

    def test_rejects_restored_business_tables_and_unqualified_names(self):
        for table in (ARN.rsplit("/", 1)[-1], ARN.replace("deletion-registry", "source-access-restore")):
            with self.subTest(table=table), self.assertRaises(ValueError):
                RecoveryRegistryInventory(self.dynamodb, live_registry_arn=table)
