"""Atomic job revisions and append-only assessments for the first synthetic menu."""

import os
import threading
from decimal import Decimal
from functools import lru_cache
from typing import Protocol

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer


class Conflict(Exception):
    pass


def _api_numbers(value):
    """Keep DynamoDB numeric values consistent with the local API contract."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, dict):
        return {key: _api_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_api_numbers(item) for item in value]
    return value


class CookingStore(Protocol):
    def read(self, pk: str) -> tuple[dict | None, dict | None]: ...
    def create(self, pk: str, meta: dict) -> None: ...
    def bind_stream(self, pk: str, expected_version: int, meta: dict, stream_pk: str) -> None: ...
    def create_recook(self, parent_pk: str, expected_parent_version: int,
                      new_pk: str, meta: dict) -> None: ...
    def commit(self, pk: str, expected_version: int, meta: dict,
               assessment: dict | None = None, pointer: dict | None = None,
               event: dict | None = None) -> None: ...
    def assessments(self, pk: str) -> list[dict]: ...
    def events(self, pk: str) -> list[dict]: ...


class MemoryCookingStore:
    """Local and contract-test store; a deployed API requires DynamoDB."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.lock = threading.RLock()

    def read(self, pk: str) -> tuple[dict | None, dict | None]:
        with self.lock:
            meta = self.items.get((pk, "META"))
            pointer = self.items.get((pk, "ASSESSMENT#CURRENT"))
            return (meta.copy() if meta else None, pointer.copy() if pointer else None)

    def create(self, pk: str, meta: dict) -> None:
        with self.lock:
            if (pk, "META") in self.items:
                raise Conflict
            self.items[(pk, "META")] = meta.copy()

    def bind_stream(self, pk: str, expected_version: int, meta: dict, stream_pk: str) -> None:
        with self.lock:
            current = self.items.get((pk, "META"))
            if not current or current["version"] != expected_version or (stream_pk, "BINDING") in self.items:
                raise Conflict
            self.items[(pk, "META")] = meta.copy()
            self.items[(stream_pk, "BINDING")] = {"pk": stream_pk, "sk": "BINDING", "job_pk": pk,
                                                   "job_id": meta["job_id"]}

    def create_recook(self, parent_pk: str, expected_parent_version: int,
                      new_pk: str, meta: dict) -> None:
        with self.lock:
            parent = self.items.get((parent_pk, "META"))
            if not parent or parent["version"] != expected_parent_version or (new_pk, "META") in self.items:
                raise Conflict
            self.items[(new_pk, "META")] = meta.copy()

    def commit(self, pk: str, expected_version: int, meta: dict,
               assessment: dict | None = None, pointer: dict | None = None,
               event: dict | None = None) -> None:
        with self.lock:
            current = self.items.get((pk, "META"))
            if not current or current["version"] != expected_version:
                raise Conflict
            if assessment and (pk, assessment["sk"]) in self.items:
                raise Conflict
            if event and (pk, event["sk"]) in self.items:
                raise Conflict
            self.items[(pk, "META")] = meta.copy()
            if assessment:
                self.items[(pk, assessment["sk"])] = assessment.copy()
            if pointer:
                self.items[(pk, "ASSESSMENT#CURRENT")] = pointer.copy()
            if event:
                self.items[(pk, event["sk"])] = event.copy()

    def assessments(self, pk: str) -> list[dict]:
        with self.lock:
            return [value.copy() for (item_pk, sk), value in sorted(self.items.items())
                    if item_pk == pk and sk.startswith("ASSESSMENT#REV#")]

    def events(self, pk: str) -> list[dict]:
        with self.lock:
            events = [value.copy() for (item_pk, sk), value in self.items.items()
                      if item_pk == pk and sk.startswith("EVENT#")]
            return sorted(events, key=lambda item: (item["occurred_at"], item["sk"]))


class DynamoCookingStore:
    def __init__(self, table_name: str) -> None:
        import boto3

        session = boto3.Session(region_name="ap-southeast-2")
        self.table = session.resource("dynamodb").Table(table_name)
        self.client = session.client("dynamodb")
        self.serializer = TypeSerializer()

    def read(self, pk: str) -> tuple[dict | None, dict | None]:
        meta = self.table.get_item(Key={"pk": pk, "sk": "META"}, ConsistentRead=True).get("Item")
        pointer = self.table.get_item(
            Key={"pk": pk, "sk": "ASSESSMENT#CURRENT"}, ConsistentRead=True,
        ).get("Item")
        return _api_numbers(meta), _api_numbers(pointer)

    def create(self, pk: str, meta: dict) -> None:
        try:
            self.table.put_item(Item={"pk": pk, "sk": "META", **meta},
                                ConditionExpression="attribute_not_exists(pk)")
        except self.client.exceptions.ConditionalCheckFailedException as exc:
            raise Conflict from exc

    def bind_stream(self, pk: str, expected_version: int, meta: dict, stream_pk: str) -> None:
        operations = [
            {"Put": {"TableName": self.table.name,
                     "Item": self._item({"pk": pk, "sk": "META", **meta}),
                     "ConditionExpression": "#version = :expected",
                     "ExpressionAttributeNames": {"#version": "version"},
                     "ExpressionAttributeValues": {":expected": {"N": str(expected_version)}}}},
            {"Put": {"TableName": self.table.name,
                     "Item": self._item({"pk": stream_pk, "sk": "BINDING", "job_pk": pk,
                                         "job_id": meta["job_id"]}),
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
                raise Conflict from exc
            raise

    def create_recook(self, parent_pk: str, expected_parent_version: int,
                      new_pk: str, meta: dict) -> None:
        operations = [
            {"ConditionCheck": {
                "TableName": self.table.name, "Key": self._item({"pk": parent_pk, "sk": "META"}),
                "ConditionExpression": "#version = :expected",
                "ExpressionAttributeNames": {"#version": "version"},
                "ExpressionAttributeValues": {":expected": {"N": str(expected_parent_version)}},
            }},
            {"Put": {"TableName": self.table.name,
                     "Item": self._item({"pk": new_pk, "sk": "META", **meta}),
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
                raise Conflict from exc
            raise

    def commit(self, pk: str, expected_version: int, meta: dict,
               assessment: dict | None = None, pointer: dict | None = None,
               event: dict | None = None) -> None:
        operations = [{"Put": {
            "TableName": self.table.name,
            "Item": self._item({"pk": pk, "sk": "META", **meta}),
            "ConditionExpression": "#version = :expected",
            "ExpressionAttributeNames": {"#version": "version"},
            "ExpressionAttributeValues": {":expected": {"N": str(expected_version)}},
        }}]
        if assessment:
            operations.append({"Put": {
                "TableName": self.table.name,
                "Item": self._item({"pk": pk, **assessment}),
                "ConditionExpression": "attribute_not_exists(pk)",
            }})
        if pointer:
            operations.append({"Put": {
                "TableName": self.table.name,
                "Item": self._item({"pk": pk, "sk": "ASSESSMENT#CURRENT", **pointer}),
            }})
        if event:
            operations.append({"Put": {
                "TableName": self.table.name,
                "Item": self._item({"pk": pk, **event}),
                "ConditionExpression": "attribute_not_exists(pk)",
            }})
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
                raise Conflict from exc
            raise

    def assessments(self, pk: str) -> list[dict]:
        results = []
        last_key = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with("ASSESSMENT#REV#"),
                    "ConsistentRead": True}
            if last_key:
                args["ExclusiveStartKey"] = last_key
            page = self.table.query(**args)
            results.extend(page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return _api_numbers(results)

    def events(self, pk: str) -> list[dict]:
        results = []
        last_key = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with("EVENT#"),
                    "ConsistentRead": True}
            if last_key:
                args["ExclusiveStartKey"] = last_key
            page = self.table.query(**args)
            results.extend(page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return _api_numbers(sorted(results, key=lambda item: (item["occurred_at"], item["sk"])))

    def _item(self, item: dict) -> dict:
        return {key: self.serializer.serialize(value) for key, value in item.items()}


@lru_cache(maxsize=4)
def _store(table_name: str) -> CookingStore:
    return DynamoCookingStore(table_name) if table_name else MemoryCookingStore()


def get_cooking_store() -> CookingStore:
    return _store(os.getenv("FOODTECH_COOKING_TABLE", ""))
