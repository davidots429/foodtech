"""Demo tier applications; the full business number is never persisted."""

import os
import threading
from functools import lru_cache
from typing import Protocol

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from fastapi import HTTPException


class TierConflict(Exception):
    pass


class TierStore(Protocol):
    def current(self, subject: str) -> dict | None: ...
    def submit(self, subject: str, item: dict) -> None: ...
    def pending(self, limit: int = 20) -> list[dict]: ...
    def decide(self, subject: str, application_id: str, reviewer: str,
               decision: str, reviewed_at: str) -> dict: ...


def _current(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in {"pk", "sk", "queue_status", "queue_sort"}}


class MemoryTierStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.lock = threading.RLock()

    def current(self, subject: str) -> dict | None:
        with self.lock:
            item = self.items.get((f"TENANT#{subject}", "CURRENT"))
            return _current(item) if item else None

    def submit(self, subject: str, item: dict) -> None:
        with self.lock:
            pk = f"TENANT#{subject}"
            old = self.items.get((pk, "CURRENT"))
            if old and old["status"] != "REJECTED":
                raise TierConflict
            current = {"pk": pk, "sk": "CURRENT", **item,
                       "queue_status": "REQUESTED", "queue_sort": item["submitted_at"] + "#" + item["application_id"]}
            self.items[(pk, "CURRENT")] = current
            self.items[(pk, f"APPLICATION#{item['application_id']}")] = {
                "pk": pk, "sk": f"APPLICATION#{item['application_id']}", **item}

    def pending(self, limit: int = 20) -> list[dict]:
        with self.lock:
            values = [item for (_, sk), item in self.items.items()
                      if sk == "CURRENT" and item.get("queue_status") == "REQUESTED"]
            return [_current(item) for item in sorted(values, key=lambda row: row["queue_sort"])[:limit]]

    def decide(self, subject: str, application_id: str, reviewer: str,
               decision: str, reviewed_at: str) -> dict:
        with self.lock:
            pk = f"TENANT#{subject}"
            old = self.items.get((pk, "CURRENT"))
            if not old or old["application_id"] != application_id or old["status"] != "REQUESTED":
                raise TierConflict
            item = {**old, "status": decision,
                    "tier": "DEMO_PAID" if decision == "APPROVED_FOR_DEMO" else "FREE",
                    "reviewed_at": reviewed_at, "reviewed_by": reviewer,
                    "version": old["version"] + 1}
            item.pop("queue_status", None)
            item.pop("queue_sort", None)
            self.items[(pk, "CURRENT")] = item
            self.items[(pk, f"REVIEW#{application_id}")] = {
                "pk": pk, "sk": f"REVIEW#{application_id}", "application_id": application_id,
                "decision": decision, "reviewed_at": reviewed_at, "reviewed_by": reviewer}
            return _current(item)


class DynamoTierStore:
    def __init__(self, table_name: str) -> None:
        import boto3

        session = boto3.Session(region_name="ap-southeast-2")
        self.table = session.resource("dynamodb").Table(table_name)
        self.client = session.client("dynamodb")
        self.serializer = TypeSerializer()

    def _item(self, item: dict) -> dict:
        return {key: self.serializer.serialize(value) for key, value in item.items()}

    def current(self, subject: str) -> dict | None:
        item = self.table.get_item(Key={"pk": f"TENANT#{subject}", "sk": "CURRENT"},
                                   ConsistentRead=True).get("Item")
        return _current(item) if item else None

    def submit(self, subject: str, item: dict) -> None:
        pk = f"TENANT#{subject}"
        current = {"pk": pk, "sk": "CURRENT", **item,
                   "queue_status": "REQUESTED", "queue_sort": item["submitted_at"] + "#" + item["application_id"]}
        application = {"pk": pk, "sk": f"APPLICATION#{item['application_id']}", **item}
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table.name, "Item": self._item(current),
                         "ConditionExpression": "attribute_not_exists(pk) OR #status = :rejected",
                         "ExpressionAttributeNames": {"#status": "status"},
                         "ExpressionAttributeValues": {":rejected": {"S": "REJECTED"}}}},
                {"Put": {"TableName": self.table.name, "Item": self._item(application),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise TierConflict from exc
            raise

    def pending(self, limit: int = 20) -> list[dict]:
        page = self.table.query(IndexName="PendingRequests", KeyConditionExpression=Key("queue_status").eq("REQUESTED"),
                                Limit=limit, ScanIndexForward=True)
        return [_current(item) for item in page.get("Items", [])]

    def decide(self, subject: str, application_id: str, reviewer: str,
               decision: str, reviewed_at: str) -> dict:
        old = self.current(subject)
        if not old or old["application_id"] != application_id or old["status"] != "REQUESTED":
            raise TierConflict
        pk = f"TENANT#{subject}"
        item = {"pk": pk, "sk": "CURRENT", **old, "status": decision,
                "tier": "DEMO_PAID" if decision == "APPROVED_FOR_DEMO" else "FREE",
                "reviewed_at": reviewed_at, "reviewed_by": reviewer,
                "version": int(old["version"]) + 1}
        review = {"pk": pk, "sk": f"REVIEW#{application_id}",
                  "application_id": application_id, "decision": decision,
                  "reviewed_at": reviewed_at, "reviewed_by": reviewer}
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table.name, "Item": self._item(item),
                         "ConditionExpression": "#status = :requested AND application_id = :application AND #version = :version",
                         "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                         "ExpressionAttributeValues": {":requested": {"S": "REQUESTED"},
                                                       ":application": {"S": application_id},
                                                       ":version": {"N": str(old["version"])}}}},
                {"Put": {"TableName": self.table.name, "Item": self._item(review),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise TierConflict from exc
            raise
        return _current(item)


@lru_cache(maxsize=4)
def _store(table_name: str) -> TierStore:
    return DynamoTierStore(table_name) if table_name else MemoryTierStore()


def get_tier_store() -> TierStore:
    table_name = os.getenv("FOODTECH_TIER_TABLE", "")
    if os.getenv("FOODTECH_SECURE_COOKIES", "true").lower() == "true" and not table_name:
        raise HTTPException(503, "시연용 티어 저장소가 설정되지 않았습니다.")
    return _store(table_name)
