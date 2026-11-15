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


def _job_list_item(pk: str, meta: dict) -> dict:
    owner = meta["owner_subject"]
    job_id = meta["job_id"]
    if pk != f"SCOPE#USER#{owner}#JOB#{job_id}":
        raise ValueError("job scope and owner do not match")
    return {"pk": f"SCOPE#USER#{owner}#JOBS",
            "sk": f"CREATED#{meta['created_at']}#{job_id}",
            "job_id": job_id, "created_at": meta["created_at"],
            "recipe_version": meta["recipe_version"],
            "planned_portions": meta["planned_portions"],
            "source_job_id": meta.get("source_job_id")}


def _only_result_already_exists(exc: Exception, operation_count: int) -> bool:
    reasons = exc.response.get("CancellationReasons", [])
    return (len(reasons) == operation_count and
            all(reason.get("Code") in (None, "None") for reason in reasons[:-1]) and
            reasons[-1].get("Code") == "ConditionalCheckFailed")


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
    def process_pointer(self, pk: str) -> dict | None: ...
    def latest_process_result(self, pk: str) -> dict | None: ...
    def create(self, pk: str, meta: dict) -> None: ...
    def list_jobs(self, subject: str, limit: int, cursor: str | None) -> dict: ...
    def ensure_listed(self, pk: str, meta: dict) -> None: ...
    def bind_stream(self, pk: str, expected_version: int, meta: dict, stream_pk: str) -> None: ...
    def create_recook(self, parent_pk: str, expected_parent_version: int,
                      new_pk: str, meta: dict) -> None: ...
    def commit(self, pk: str, expected_version: int, meta: dict,
               assessment: dict | None = None, pointer: dict | None = None,
               event: dict | None = None) -> None: ...
    def assessments(self, pk: str) -> list[dict]: ...
    def events(self, pk: str) -> list[dict]: ...
    def assessment(self, pk: str, version: int) -> dict | None: ...
    def comparison(self, pk: str, comparison_id: str) -> dict | None: ...
    def list_comparison_results(self, candidate_pk: str, limit: int, cursor: str | None) -> dict: ...
    def list_current_comparisons(self, candidate_pk: str, limit: int, cursor: str | None) -> dict: ...
    def comparison_dependencies(self, job_pk: str) -> list[dict]: ...
    def current_comparison(self, candidate_pk: str, baseline_id: str) -> dict | None: ...
    def save_comparison(self, baseline_pk: str, candidate_pk: str,
                        baseline_meta: dict, candidate_meta: dict,
                        baseline_pointer: dict, candidate_pointer: dict,
                        record: dict) -> dict: ...
    def aggregate(self, pk: str, result_id: str) -> dict | None: ...
    def list_aggregate_results(self, pk: str, limit: int, cursor: str | None) -> dict: ...
    def aggregate_dependencies(self, job_pk: str) -> list[dict]: ...
    def list_aggregate_dependencies(self, job_pk: str, limit: int, cursor: str | None) -> dict: ...
    def current_aggregate(self, pk: str) -> dict | None: ...
    def save_aggregate(self, pk: str, snapshots: list[tuple[str, dict, dict | None, dict | None]],
                       record: dict) -> dict: ...


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

    def process_pointer(self, pk: str) -> dict | None:
        with self.lock:
            item = self.items.get((pk, "PROCESS#LATEST_RESTATED"))
            return item.copy() if item else None

    def latest_process_result(self, pk: str) -> dict | None:
        with self.lock:
            pointer = self.items.get((pk, "PROCESS#LATEST_RESTATED"))
            if not pointer:
                return None
            result = self.items.get((pk, pointer["result_sk"]))
            return result.copy() if result else None

    def create(self, pk: str, meta: dict) -> None:
        with self.lock:
            listing = _job_list_item(pk, meta)
            list_key = (listing["pk"], listing["sk"])
            if (pk, "META") in self.items or list_key in self.items:
                raise Conflict
            self.items[(pk, "META")] = meta.copy()
            self.items[list_key] = listing

    def list_jobs(self, subject: str, limit: int, cursor: str | None) -> dict:
        with self.lock:
            pk = f"SCOPE#USER#{subject}#JOBS"
            entries = [item.copy() for (item_pk, sk), item in self.items.items()
                       if item_pk == pk and sk.startswith("CREATED#") and
                       (cursor is None or sk < cursor)]
            entries.sort(key=lambda item: item["sk"], reverse=True)
            page = entries[:limit]
            return {"items": [{key: value for key, value in item.items() if key not in {"pk", "sk"}}
                              for item in page],
                    "next_cursor": page[-1]["sk"] if len(entries) > limit else None}

    def ensure_listed(self, pk: str, meta: dict) -> None:
        with self.lock:
            listing = _job_list_item(pk, meta)
            if (pk, "META") not in self.items:
                raise Conflict
            self.items.setdefault((listing["pk"], listing["sk"]), listing)

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
            listing = _job_list_item(new_pk, meta)
            list_key = (listing["pk"], listing["sk"])
            if not parent or parent["version"] != expected_parent_version or \
                    (new_pk, "META") in self.items or list_key in self.items:
                raise Conflict
            self.items[(new_pk, "META")] = meta.copy()
            self.items[list_key] = listing

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

    def assessment(self, pk: str, version: int) -> dict | None:
        with self.lock:
            item = self.items.get((pk, f"ASSESSMENT#REV#{version:012d}"))
            return item.copy() if item else None

    def comparison(self, pk: str, comparison_id: str) -> dict | None:
        with self.lock:
            item = self.items.get((pk, f"COMPARISON#{comparison_id}"))
            return item.copy() if item else None

    def list_comparison_results(self, candidate_pk: str, limit: int, cursor: str | None) -> dict:
        prefix = "COMPARISON#"
        with self.lock:
            records = [item.copy() for (pk, sk), item in sorted(self.items.items())
                       if pk == candidate_pk and len(sk) == len(prefix) + 64 and
                       all(char in "0123456789abcdef" for char in sk[len(prefix):]) and
                       (cursor is None or sk > prefix + cursor)]
        fields = ("comparison_id", "baseline_job_id", "compared_at", "score_total_delta",
                  "improvement_status")
        shown = records[:limit]
        return {"items": [{key: item[key] for key in fields} for item in shown],
                "next_cursor": shown[-1]["comparison_id"] if len(records) > limit else None}

    def list_current_comparisons(self, candidate_pk: str, limit: int, cursor: str | None) -> dict:
        prefix = "COMPARISON#CURRENT#"
        with self.lock:
            pointers = [item.copy() for (pk, sk), item in self.items.items()
                        if pk == candidate_pk and sk.startswith(prefix) and
                        (cursor is None or sk > prefix + cursor)]
            pointers.sort(key=lambda item: item["sk"])
            page = pointers[:limit]
            return {"items": [{"baseline_job_id": item["sk"].removeprefix(prefix),
                               "comparison_id": item["comparison_id"],
                               "updated_at": item["updated_at"]} for item in page],
                    "next_cursor": page[-1]["sk"].removeprefix(prefix) if len(pointers) > limit else None}

    def comparison_dependencies(self, job_pk: str) -> list[dict]:
        with self.lock:
            return [item.copy() for (pk, sk), item in sorted(self.items.items())
                    if pk == job_pk and sk.startswith("COMPARISON#DEP#")]

    def current_comparison(self, candidate_pk: str, baseline_id: str) -> dict | None:
        with self.lock:
            item = self.items.get((candidate_pk, f"COMPARISON#CURRENT#{baseline_id}"))
            return item.copy() if item else None

    def save_comparison(self, baseline_pk: str, candidate_pk: str,
                        baseline_meta: dict, candidate_meta: dict,
                        baseline_pointer: dict, candidate_pointer: dict,
                        record: dict) -> dict:
        with self.lock:
            key = (candidate_pk, f"COMPARISON#{record['comparison_id']}")
            for pk, meta, pointer in ((baseline_pk, baseline_meta, baseline_pointer),
                                      (candidate_pk, candidate_meta, candidate_pointer)):
                current_meta = self.items.get((pk, "META"))
                current_pointer = self.items.get((pk, "ASSESSMENT#CURRENT"))
                if not current_meta or not current_pointer or \
                        current_meta["version"] != meta["version"] or \
                        current_meta["status"] != "COMPLETED" or \
                        current_meta["safety_status"] != "PASSED" or \
                        current_meta["evaluation_blocked"] or \
                        current_pointer["version"] != pointer["version"] or \
                        not current_pointer["usable"] or \
                        current_pointer["safety_version"] != meta["safety_version"]:
                    raise Conflict
            if key not in self.items:
                self.items[key] = {"sk": key[1], **record}
                self.items[(candidate_pk, f"COMPARISON#CURRENT#{record['baseline_job_id']}")] = {
                    "pk": candidate_pk, "sk": f"COMPARISON#CURRENT#{record['baseline_job_id']}",
                    "comparison_id": record["comparison_id"], "updated_at": record["compared_at"]}
            elif (candidate_pk, f"COMPARISON#CURRENT#{record['baseline_job_id']}") not in self.items:
                self.items[(candidate_pk, f"COMPARISON#CURRENT#{record['baseline_job_id']}")] = {
                    "pk": candidate_pk, "sk": f"COMPARISON#CURRENT#{record['baseline_job_id']}",
                    "comparison_id": record["comparison_id"], "updated_at": self.items[key]["compared_at"]}
            dependency_sk = f"COMPARISON#DEP#{record['candidate_job_id']}#{record['baseline_job_id']}"
            for job_pk in (baseline_pk, candidate_pk):
                self.items[(job_pk, dependency_sk)] = {
                    "pk": job_pk, "sk": dependency_sk, "baseline_pk": baseline_pk,
                    "candidate_pk": candidate_pk}
            return self.items[key].copy()

    def aggregate(self, pk: str, result_id: str) -> dict | None:
        with self.lock:
            item = self.items.get((pk, f"RESULT#{result_id}"))
            return item.copy() if item else None

    def list_aggregate_results(self, pk: str, limit: int, cursor: str | None) -> dict:
        prefix = "RESULT#"
        with self.lock:
            entries = [item.copy() for (item_pk, sk), item in self.items.items()
                       if item_pk == pk and sk.startswith(prefix) and
                       (cursor is None or sk > prefix + cursor)]
            entries.sort(key=lambda item: item["sk"])
            page = entries[:limit]
            fields = ("result_id", "created_at", "sample_count", "excluded_count",
                      "mean_quality_score_percent")
            return {"items": [{key: item[key] for key in fields} for item in page],
                    "next_cursor": page[-1]["sk"].removeprefix(prefix) if len(entries) > limit else None}

    def aggregate_dependencies(self, job_pk: str) -> list[dict]:
        with self.lock:
            return [item.copy() for (pk, sk), item in sorted(self.items.items())
                    if pk == job_pk and sk.startswith("AGGREGATE#DEP#")]

    def list_aggregate_dependencies(self, job_pk: str, limit: int, cursor: str | None) -> dict:
        prefix = "AGGREGATE#DEP#"
        with self.lock:
            entries = [item.copy() for (pk, sk), item in self.items.items()
                       if pk == job_pk and sk.startswith(prefix) and
                       (cursor is None or sk > prefix + cursor)]
            entries.sort(key=lambda item: item["sk"])
            page = entries[:limit]
            return {"items": page,
                    "next_cursor": page[-1]["sk"].removeprefix(prefix) if len(entries) > limit else None}

    def current_aggregate(self, pk: str) -> dict | None:
        with self.lock:
            pointer = self.items.get((pk, "CURRENT"))
            return pointer.copy() if pointer else None

    def save_aggregate(self, pk: str, snapshots: list[tuple[str, dict, dict | None, dict | None]],
                       record: dict) -> dict:
        with self.lock:
            for job_pk, meta, pointer, process_pointer in snapshots:
                current_meta = self.items.get((job_pk, "META"))
                current_pointer = self.items.get((job_pk, "ASSESSMENT#CURRENT"))
                current_process = self.items.get((job_pk, "PROCESS#LATEST_RESTATED"))
                if not current_meta or any(current_meta.get(field) != meta.get(field) for field in
                                           ("version", "status", "safety_status", "evaluation_blocked")) or \
                        (current_pointer is None) != (pointer is None) or \
                        (pointer is not None and any(current_pointer.get(field) != pointer.get(field) for field in
                                                     ("version", "usable", "safety_version"))) or \
                        (current_process is None) != (process_pointer is None) or \
                        (process_pointer is not None and any(current_process.get(field) != process_pointer.get(field)
                                                             for field in ("version", "result_sk"))):
                    raise Conflict
            key = (pk, f"RESULT#{record['result_id']}")
            created = key not in self.items
            if created:
                self.items[key] = {"sk": key[1], **record}
            if created or (pk, "CURRENT") not in self.items:
                self.items[(pk, "CURRENT")] = {"pk": pk, "sk": "CURRENT",
                                                "result_id": self.items[key]["result_id"],
                                                "updated_at": self.items[key]["created_at"]}
            for job_pk, _, _, _ in snapshots:
                dependency_sk = f"AGGREGATE#DEP#{record['group_id']}"
                self.items[(job_pk, dependency_sk)] = {
                    "pk": job_pk, "sk": dependency_sk, "aggregate_pk": pk,
                    "selection": record["selection"]}
            return self.items[key].copy()


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

    def process_pointer(self, pk: str) -> dict | None:
        item = self.table.get_item(Key={"pk": pk, "sk": "PROCESS#LATEST_RESTATED"},
                                   ConsistentRead=True).get("Item")
        return _api_numbers(item)

    def latest_process_result(self, pk: str) -> dict | None:
        pointer = self.table.get_item(Key={"pk": pk, "sk": "PROCESS#LATEST_RESTATED"},
                                      ConsistentRead=True).get("Item")
        if not pointer:
            return None
        result_sk = pointer.get("result_sk", "")
        if not result_sk.startswith("RESTATEMENT#RESULT#"):
            raise ValueError("invalid latest process result pointer")
        result = self.table.get_item(Key={"pk": pk, "sk": result_sk},
                                     ConsistentRead=True).get("Item")
        if not result or result.get("status") != "DERIVED_ONLY":
            raise ValueError("latest process result is missing")
        return _api_numbers(result)

    def create(self, pk: str, meta: dict) -> None:
        listing = _job_list_item(pk, meta)
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table.name,
                         "Item": self._item({"pk": pk, "sk": "META", **meta}),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Put": {"TableName": self.table.name, "Item": self._item(listing),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise Conflict from exc
            raise

    def list_jobs(self, subject: str, limit: int, cursor: str | None) -> dict:
        pk = f"SCOPE#USER#{subject}#JOBS"
        args = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with("CREATED#"),
                "ScanIndexForward": False, "ConsistentRead": True, "Limit": limit}
        if cursor:
            args["ExclusiveStartKey"] = {"pk": pk, "sk": cursor}
        page = self.table.query(**args)
        return {"items": [_api_numbers({key: value for key, value in item.items()
                                        if key not in {"pk", "sk"}}) for item in page.get("Items", [])],
                "next_cursor": page.get("LastEvaluatedKey", {}).get("sk")}

    def ensure_listed(self, pk: str, meta: dict) -> None:
        listing = _job_list_item(pk, meta)
        try:
            self.table.put_item(Item=listing, ConditionExpression="attribute_not_exists(pk)")
        except self.client.exceptions.ConditionalCheckFailedException:
            return

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
        listing = _job_list_item(new_pk, meta)
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
            {"Put": {"TableName": self.table.name, "Item": self._item(listing),
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

    def assessment(self, pk: str, version: int) -> dict | None:
        item = self.table.get_item(Key={"pk": pk, "sk": f"ASSESSMENT#REV#{version:012d}"},
                                   ConsistentRead=True).get("Item")
        return _api_numbers(item)

    def comparison(self, pk: str, comparison_id: str) -> dict | None:
        item = self.table.get_item(Key={"pk": pk, "sk": f"COMPARISON#{comparison_id}"},
                                   ConsistentRead=True).get("Item")
        return _api_numbers({key: value for key, value in item.items() if key != "pk"}) if item else None

    def list_comparison_results(self, candidate_pk: str, limit: int, cursor: str | None) -> dict:
        prefix = "COMPARISON#"
        args = {"KeyConditionExpression": Key("pk").eq(candidate_pk) & Key("sk").begins_with(prefix),
                "ConsistentRead": True, "Limit": limit + 1}
        if cursor:
            args["ExclusiveStartKey"] = {"pk": candidate_pk, "sk": prefix + cursor}
        records = []
        while len(records) <= limit:
            page = self.table.query(**args)
            records.extend(item for item in page.get("Items", [])
                           if len(item["sk"]) == len(prefix) + 64 and
                           all(char in "0123456789abcdef" for char in item["sk"][len(prefix):]))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                break
            args["ExclusiveStartKey"] = last_key
        fields = ("comparison_id", "baseline_job_id", "compared_at", "score_total_delta",
                  "improvement_status")
        shown = records[:limit]
        return {"items": [_api_numbers({key: item[key] for key in fields}) for item in shown],
                "next_cursor": shown[-1]["comparison_id"] if len(records) > limit else None}

    def list_current_comparisons(self, candidate_pk: str, limit: int, cursor: str | None) -> dict:
        prefix = "COMPARISON#CURRENT#"
        args = {"KeyConditionExpression": Key("pk").eq(candidate_pk) & Key("sk").begins_with(prefix),
                "ConsistentRead": True, "Limit": limit}
        if cursor:
            args["ExclusiveStartKey"] = {"pk": candidate_pk, "sk": prefix + cursor}
        page = self.table.query(**args)
        return {"items": [{"baseline_job_id": item["sk"].removeprefix(prefix),
                           "comparison_id": item["comparison_id"],
                           "updated_at": item["updated_at"]} for item in page.get("Items", [])],
                "next_cursor": page.get("LastEvaluatedKey", {}).get("sk", "").removeprefix(prefix) or None}

    def comparison_dependencies(self, job_pk: str) -> list[dict]:
        items = []
        last_key = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(job_pk) &
                    Key("sk").begins_with("COMPARISON#DEP#"), "ConsistentRead": True}
            if last_key:
                args["ExclusiveStartKey"] = last_key
            page = self.table.query(**args)
            items.extend(_api_numbers(item) for item in page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return items

    def current_comparison(self, candidate_pk: str, baseline_id: str) -> dict | None:
        item = self.table.get_item(Key={"pk": candidate_pk,
                                        "sk": f"COMPARISON#CURRENT#{baseline_id}"},
                                   ConsistentRead=True).get("Item")
        return _api_numbers(item)

    def save_comparison(self, baseline_pk: str, candidate_pk: str,
                        baseline_meta: dict, candidate_meta: dict,
                        baseline_pointer: dict, candidate_pointer: dict,
                        record: dict) -> dict:
        operations = []
        for pk, meta, pointer in ((baseline_pk, baseline_meta, baseline_pointer),
                                  (candidate_pk, candidate_meta, candidate_pointer)):
            operations.extend([
                {"ConditionCheck": {"TableName": self.table.name,
                                    "Key": self._item({"pk": pk, "sk": "META"}),
                                    "ConditionExpression": "#v = :v AND #s = :completed AND safety_status = :passed AND evaluation_blocked = :false",
                                    "ExpressionAttributeNames": {"#v": "version", "#s": "status"},
                                    "ExpressionAttributeValues": self._item({":v": int(meta["version"]),
                                                                              ":completed": "COMPLETED", ":passed": "PASSED",
                                                                              ":false": False})}},
                {"ConditionCheck": {"TableName": self.table.name,
                                    "Key": self._item({"pk": pk, "sk": "ASSESSMENT#CURRENT"}),
                                    "ConditionExpression": "#v = :v AND usable = :true AND safety_version = :safety",
                                    "ExpressionAttributeNames": {"#v": "version"},
                                    "ExpressionAttributeValues": self._item({":v": int(pointer["version"]),
                                                                              ":true": True, ":safety": int(meta["safety_version"])})}},
            ])
        dependency_sk = f"COMPARISON#DEP#{record['candidate_job_id']}#{record['baseline_job_id']}"
        for job_pk in (baseline_pk, candidate_pk):
            operations.append({"Put": {"TableName": self.table.name,
                                       "Item": self._item({"pk": job_pk, "sk": dependency_sk,
                                                           "baseline_pk": baseline_pk,
                                                           "candidate_pk": candidate_pk})}})
        operations.append({"Put": {"TableName": self.table.name,
                                   "Item": self._item({"pk": candidate_pk,
                                                       "sk": f"COMPARISON#CURRENT#{record['baseline_job_id']}",
                                                       "comparison_id": record["comparison_id"],
                                                       "updated_at": record["compared_at"]})}})
        operations.append({"Put": {"TableName": self.table.name,
                                   "Item": self._item({"pk": candidate_pk,
                                                       "sk": f"COMPARISON#{record['comparison_id']}", **record}),
                                   "ConditionExpression": "attribute_not_exists(pk)"}})
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if _only_result_already_exists(exc, len(operations)):
                existing = self.comparison(candidate_pk, record["comparison_id"])
                if existing and existing.get("baseline_job_id") == record["baseline_job_id"] and \
                        existing.get("candidate_job_id") == record["candidate_job_id"]:
                    self._repair_comparison_index(candidate_pk, operations[:-2], existing)
                    return existing
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise Conflict from exc
            raise
        return {"sk": f"COMPARISON#{record['comparison_id']}", **record}

    def _repair_comparison_index(self, candidate_pk: str, job_operations: list[dict],
                                 existing: dict) -> None:
        operations = [*job_operations,
                      {"ConditionCheck": {
                          "TableName": self.table.name,
                          "Key": self._item({"pk": candidate_pk,
                                              "sk": f"COMPARISON#{existing['comparison_id']}"}),
                          "ConditionExpression": "#id = :id",
                          "ExpressionAttributeNames": {"#id": "comparison_id"},
                          "ExpressionAttributeValues": self._item({":id": existing["comparison_id"]}),
                      }},
                      {"Update": {
                          "TableName": self.table.name,
                          "Key": self._item({"pk": candidate_pk,
                                              "sk": f"COMPARISON#CURRENT#{existing['baseline_job_id']}"}),
                          "UpdateExpression": "SET #id = if_not_exists(#id, :id), "
                                              "#updated = if_not_exists(#updated, :updated)",
                          "ExpressionAttributeNames": {"#id": "comparison_id", "#updated": "updated_at"},
                          "ExpressionAttributeValues": self._item({":id": existing["comparison_id"],
                                                                     ":updated": existing["compared_at"]}),
                      }}]
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise Conflict from exc
            raise

    def aggregate(self, pk: str, result_id: str) -> dict | None:
        item = self.table.get_item(Key={"pk": pk, "sk": f"RESULT#{result_id}"},
                                   ConsistentRead=True).get("Item")
        return _api_numbers({key: value for key, value in item.items() if key != "pk"}) if item else None

    def list_aggregate_results(self, pk: str, limit: int, cursor: str | None) -> dict:
        prefix = "RESULT#"
        args = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix),
                "ConsistentRead": True, "Limit": limit + 1}
        if cursor:
            args["ExclusiveStartKey"] = {"pk": pk, "sk": prefix + cursor}
        page = self.table.query(**args)
        fields = ("result_id", "created_at", "sample_count", "excluded_count",
                  "mean_quality_score_percent")
        entries = page.get("Items", [])
        shown = entries[:limit]
        has_more = len(entries) > limit or bool(page.get("LastEvaluatedKey"))
        return {"items": [_api_numbers({key: item[key] for key in fields}) for item in shown],
                "next_cursor": shown[-1]["result_id"] if shown and has_more else None}

    def aggregate_dependencies(self, job_pk: str) -> list[dict]:
        items = []
        last_key = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(job_pk) &
                    Key("sk").begins_with("AGGREGATE#DEP#"), "ConsistentRead": True}
            if last_key:
                args["ExclusiveStartKey"] = last_key
            page = self.table.query(**args)
            items.extend(_api_numbers(item) for item in page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return items

    def list_aggregate_dependencies(self, job_pk: str, limit: int, cursor: str | None) -> dict:
        prefix = "AGGREGATE#DEP#"
        args = {"KeyConditionExpression": Key("pk").eq(job_pk) & Key("sk").begins_with(prefix),
                "ConsistentRead": True, "Limit": limit}
        if cursor:
            args["ExclusiveStartKey"] = {"pk": job_pk, "sk": prefix + cursor}
        page = self.table.query(**args)
        return {"items": [_api_numbers(item) for item in page.get("Items", [])],
                "next_cursor": page.get("LastEvaluatedKey", {}).get("sk", "").removeprefix(prefix) or None}

    def current_aggregate(self, pk: str) -> dict | None:
        item = self.table.get_item(Key={"pk": pk, "sk": "CURRENT"},
                                   ConsistentRead=True).get("Item")
        return _api_numbers(item)

    def save_aggregate(self, pk: str, snapshots: list[tuple[str, dict, dict | None, dict | None]],
                       record: dict) -> dict:
        operations = []
        for job_pk, meta, pointer, process_pointer in snapshots:
            operations.append({"ConditionCheck": {
                "TableName": self.table.name, "Key": self._item({"pk": job_pk, "sk": "META"}),
                "ConditionExpression": "#v = :v AND #s = :s AND safety_status = :safety AND evaluation_blocked = :blocked",
                "ExpressionAttributeNames": {"#v": "version", "#s": "status"},
                "ExpressionAttributeValues": self._item({":v": int(meta["version"]), ":s": meta["status"],
                                                          ":safety": meta["safety_status"],
                                                          ":blocked": meta["evaluation_blocked"]}),
            }})
            if pointer is None:
                operations.append({"ConditionCheck": {
                    "TableName": self.table.name,
                    "Key": self._item({"pk": job_pk, "sk": "ASSESSMENT#CURRENT"}),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }})
            else:
                operations.append({"ConditionCheck": {
                    "TableName": self.table.name,
                    "Key": self._item({"pk": job_pk, "sk": "ASSESSMENT#CURRENT"}),
                    "ConditionExpression": "#v = :v AND usable = :usable AND safety_version = :safety",
                    "ExpressionAttributeNames": {"#v": "version"},
                    "ExpressionAttributeValues": self._item({":v": int(pointer["version"]),
                                                              ":usable": pointer["usable"],
                                                              ":safety": int(pointer["safety_version"])}),
                }})
            if process_pointer is None:
                operations.append({"ConditionCheck": {
                    "TableName": self.table.name,
                    "Key": self._item({"pk": job_pk, "sk": "PROCESS#LATEST_RESTATED"}),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }})
            else:
                operations.append({"ConditionCheck": {
                    "TableName": self.table.name,
                    "Key": self._item({"pk": job_pk, "sk": "PROCESS#LATEST_RESTATED"}),
                    "ConditionExpression": "#v = :v AND result_sk = :result",
                    "ExpressionAttributeNames": {"#v": "version"},
                    "ExpressionAttributeValues": self._item({":v": int(process_pointer["version"]),
                                                              ":result": process_pointer["result_sk"]}),
                }})
            dependency_sk = f"AGGREGATE#DEP#{record['group_id']}"
            operations.append({"Put": {"TableName": self.table.name,
                                       "Item": self._item({"pk": job_pk, "sk": dependency_sk,
                                                           "aggregate_pk": pk,
                                                           "selection": record["selection"]})}})
        operations.append({"Put": {"TableName": self.table.name,
                                   "Item": self._item({"pk": pk, "sk": "CURRENT",
                                                       "result_id": record["result_id"],
                                                       "updated_at": record["created_at"]})}})
        operations.append({"Put": {"TableName": self.table.name,
                                   "Item": self._item({"pk": pk, "sk": f"RESULT#{record['result_id']}", **record}),
                                   "ConditionExpression": "attribute_not_exists(pk)"}})
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if _only_result_already_exists(exc, len(operations)):
                existing = self.aggregate(pk, record["result_id"])
                if existing and existing.get("group_id") == record["group_id"] and \
                        existing.get("selection") == record["selection"]:
                    self._repair_aggregate_index(pk, operations[:-2], existing)
                    return existing
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise Conflict from exc
            raise
        return {"sk": f"RESULT#{record['result_id']}", **record}

    def _repair_aggregate_index(self, pk: str, job_operations: list[dict], existing: dict) -> None:
        """Backfill a pre-index result without replacing a newer current pointer."""
        operations = [*job_operations,
                      {"ConditionCheck": {
                          "TableName": self.table.name,
                          "Key": self._item({"pk": pk, "sk": f"RESULT#{existing['result_id']}"}),
                          "ConditionExpression": "#g = :group AND #r = :result",
                          "ExpressionAttributeNames": {"#g": "group_id", "#r": "result_id"},
                          "ExpressionAttributeValues": self._item({":group": existing["group_id"],
                                                                     ":result": existing["result_id"]}),
                      }},
                      {"Update": {
                          "TableName": self.table.name,
                          "Key": self._item({"pk": pk, "sk": "CURRENT"}),
                          "UpdateExpression": "SET #r = if_not_exists(#r, :result), "
                                              "#updated = if_not_exists(#updated, :updated)",
                          "ExpressionAttributeNames": {"#r": "result_id", "#updated": "updated_at"},
                          "ExpressionAttributeValues": self._item({":result": existing["result_id"],
                                                                     ":updated": existing["created_at"]}),
                      }}]
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise Conflict from exc
            raise

    def _item(self, item: dict) -> dict:
        return {key: self.serializer.serialize(value) for key, value in item.items()}


@lru_cache(maxsize=4)
def _store(table_name: str) -> CookingStore:
    return DynamoCookingStore(table_name) if table_name else MemoryCookingStore()


def get_cooking_store() -> CookingStore:
    return _store(os.getenv("FOODTECH_COOKING_TABLE", ""))
