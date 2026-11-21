"""Atomic cooking job revisions and append-only assessments."""

import os
import random
import re
import threading
import time
from decimal import Decimal
from functools import lru_cache
from typing import Protocol

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer


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
            "menu": meta.get("menu", "TONKATSU"),
            "equipment_profile": meta.get("equipment_profile", "synthetic-fryer"),
            "recipe_version": meta["recipe_version"],
            "planned_portions": meta["planned_portions"],
            "source_job_id": meta.get("source_job_id")}


def _history_pk(job_pk: str) -> str:
    prefix, separator, job_id = job_pk.rpartition("#JOB#")
    if not separator or not prefix.startswith("SCOPE#USER#") or not job_id:
        raise ValueError("invalid scoped job key")
    return prefix + "#JOBS"


def _history_number(item: dict | None) -> int | None:
    if item is None:
        return None
    value = item.get("revision")
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or \
            (isinstance(value, Decimal) and not value.is_finite()) or not 1 <= value <= 10**18 or int(value) != value:
        raise ValueError("invalid job history revision")
    return int(value)


def _history_relevant(event: dict | None) -> bool:
    """Track changes that can alter a job's reusable safety evidence."""
    return bool(event and (event.get("trigger") == "COOKING_STARTED" or event.get("domain") in {
        "VALIDATION_CONTEXT", "SAFETY_STATUS", "JEYUK_FEED", "JEYUK_DISCHARGE",
        "TONKATSU_BATCH", "TONKATSU_INITIAL_CONTEXT", "COOKING_JOB_STATUS",
    }))


def _evidence_snapshot_inputs(pk: str, expected_version: int, snapshot: dict,
                              source_table: str, revision: int, receipt: dict) -> None:
    _history_pk(pk)
    if type(expected_version) is not int or expected_version < 1 or type(revision) is not int or revision < 1 or \
            not isinstance(source_table, str) or not re.fullmatch(r"foodtech-data-flow-[a-z0-9-]{1,32}", source_table):
        raise ValueError("invalid evidence snapshot scope")
    if not isinstance(snapshot, dict) or "pk" in snapshot or snapshot.get("status") != "LINKED_UNVERIFIED" or \
            snapshot.get("job_version") != expected_version or snapshot.get("source_revision") != revision or \
            snapshot.get("source_receipt_reference") != receipt or \
            not isinstance(snapshot.get("sk"), str) or not re.fullmatch(r"VALIDATION#EVIDENCE#[0-9a-f-]{36}", snapshot["sk"]):
        raise ValueError("invalid evidence snapshot")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("pk"), str) or \
            not re.fullmatch(r"JEYUK#[A-Za-z0-9_-]{1,128}", receipt["pk"]) or \
            not isinstance(receipt.get("sk"), str) or not receipt["sk"].startswith("COLLECTION#RECEIPT#") or \
            receipt.get("record_count") != revision or not isinstance(receipt.get("ordered_raw_sha256"), str) or \
            not re.fullmatch(r"[0-9a-f]{64}", receipt["ordered_raw_sha256"]):
        raise ValueError("invalid evidence receipt reference")


def _only_result_already_exists(exc: Exception, operation_count: int) -> bool:
    reasons = exc.response.get("CancellationReasons", [])
    return (len(reasons) == operation_count and
            all(reason.get("Code") in (None, "None") for reason in reasons[:-1]) and
            reasons[-1].get("Code") == "ConditionalCheckFailed")


def _is_transaction_contention(exc: Exception) -> bool:
    reasons = exc.response.get("CancellationReasons", [])
    codes = {reason.get("Code") for reason in reasons}
    expected = {"ConditionalCheckFailed", "TransactionConflict"}
    return bool(codes & expected) and codes <= expected | {None, "None"}


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
    def validation_evidence_snapshots(self, pk: str) -> list[dict]: ...
    def save_validation_evidence(self, pk: str, expected_version: int, snapshot: dict,
                                 source_table: str, revision: int, receipt: dict) -> None: ...
    def customer_restatement(self, pk: str, request_id: str) -> dict | None: ...
    def create_customer_restatement(self, pk: str, expected_version: int, request: dict) -> dict: ...
    def create_meal(self, pk: str, records: list[dict]) -> None: ...
    def read_meal(self, pk: str) -> list[dict]: ...
    def advance_meal(self, pk: str, expected_version: int, meta: dict,
                     step: dict, event: dict) -> None: ...
    def attach_meal_source(self, pk: str, expected_version: int, meta: dict,
                           event: dict, chunks: list[dict], live_count: int | None = None) -> None: ...
    def read_meal_source_chunk(self, pk: str, index: int) -> dict | None: ...
    def read_meal_live(self, pk: str) -> dict | None: ...
    def read_meal_live_chunk(self, pk: str, start: int) -> dict | None: ...
    def read_meal_live_chunks(self, pk: str) -> list[dict]: ...
    def append_meal_live(self, pk: str, expected: int, state: dict, chunk: dict) -> None: ...
    def read(self, pk: str) -> tuple[dict | None, dict | None]: ...
    def read_meta(self, pk: str) -> dict | None: ...
    def process_pointer(self, pk: str) -> dict | None: ...
    def latest_process_result(self, pk: str) -> dict | None: ...
    def create(self, pk: str, meta: dict) -> None: ...
    def list_jobs(self, subject: str, limit: int, cursor: str | None) -> dict: ...
    def history_revision(self, subject: str) -> int | None: ...
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
        self.validation_sources: dict[tuple[str, str], dict] = {}
        self.lock = threading.RLock()

    def save_validation_evidence(self, pk: str, expected_version: int, snapshot: dict,
                                 source_table: str, revision: int, receipt: dict) -> None:
        _evidence_snapshot_inputs(pk, expected_version, snapshot, source_table, revision, receipt)
        with self.lock:
            job = self.items.get((pk, "META"))
            source = self.validation_sources.get((source_table, receipt["pk"]))
            if not job or job.get("version") != expected_version or not source or \
                    source.get("revision") != revision or source.get("receipt") != receipt or \
                    (pk, snapshot["sk"]) in self.items:
                raise Conflict
            self.items[(pk, snapshot["sk"])] = {"pk": pk, **snapshot}

    def validation_evidence_snapshots(self, pk: str) -> list[dict]:
        with self.lock:
            return [value.copy() for (item_pk, sk), value in sorted(self.items.items())
                    if item_pk == pk and sk.startswith("VALIDATION#EVIDENCE#")]

    def customer_restatement(self, pk: str, request_id: str) -> dict | None:
        with self.lock:
            value = self.items.get((pk, f"RESTATEMENT#REQUEST#{request_id}"))
            return value.copy() if value else None

    def create_customer_restatement(self, pk: str, expected_version: int, request: dict) -> dict:
        key = (pk, request["sk"])
        with self.lock:
            existing = self.items.get(key)
            if existing:
                if any(existing.get(field) != request.get(field) for field in
                       ("reason", "request_id", "job_version_at_detection", "source_stream_pk")):
                    raise Conflict
                return existing.copy()
            current = self.items.get((pk, "META"))
            if not current or current["version"] != expected_version:
                raise Conflict
            self.items[key] = {"pk": pk, **request}
            return self.items[key].copy()

    def create_meal(self, pk: str, records: list[dict]) -> None:
        with self.lock:
            if any((pk, row["sk"]) in self.items for row in records):
                raise Conflict
            for row in records:
                self.items[(pk, row["sk"])] = {"pk": pk, **row}

    def read_meal(self, pk: str) -> list[dict]:
        with self.lock:
            return [item.copy() for (item_pk, _), item in sorted(self.items.items()) if item_pk == pk]

    def advance_meal(self, pk: str, expected_version: int, meta: dict,
                     step: dict, event: dict) -> None:
        with self.lock:
            current = self.items.get((pk, "META"))
            if (not current or current["version"] != expected_version or
                    (pk, step["sk"]) not in self.items or (pk, event["sk"]) in self.items):
                raise Conflict
            for row in (meta, step, event):
                self.items[(pk, row["sk"])] = {"pk": pk, **row}

    def attach_meal_source(self, pk: str, expected_version: int, meta: dict,
                           event: dict, chunks: list[dict], live_count: int | None = None) -> None:
        source_pk = pk + "#SOURCE"
        with self.lock:
            current = self.items.get((pk, "META"))
            live = self.items.get((pk + "#LIVE", "META"))
            if (not current or current["version"] != expected_version or (pk, event["sk"]) in self.items or
                    (int(live["count"]) if live else None) != live_count or
                    any((source_pk, row["sk"]) in self.items for row in chunks)):
                raise Conflict
            self.items[(pk, "META")] = {"pk": pk, **meta}
            self.items[(pk, event["sk"])] = {"pk": pk, **event}
            for row in chunks:
                self.items[(source_pk, row["sk"])] = {"pk": source_pk, **row}

    def read_meal_source_chunk(self, pk: str, index: int) -> dict | None:
        with self.lock:
            row = self.items.get((pk + "#SOURCE", f"RAW#{index:04d}"))
            return row.copy() if row else None

    def read_meal_live(self, pk: str) -> dict | None:
        with self.lock:
            row = self.items.get((pk + "#LIVE", "META"))
            return row.copy() if row else None

    def read_meal_live_chunk(self, pk: str, start: int) -> dict | None:
        with self.lock:
            row = self.items.get((pk + "#LIVE", f"CHUNK#{start:06d}"))
            return row.copy() if row else None

    def read_meal_live_chunks(self, pk: str) -> list[dict]:
        with self.lock:
            return [row.copy() for (item_pk, sk), row in sorted(self.items.items())
                    if item_pk == pk + "#LIVE" and sk.startswith("CHUNK#")]

    def append_meal_live(self, pk: str, expected: int, state: dict, chunk: dict) -> None:
        with self.lock:
            meal = self.items.get((pk, "META"))
            current = self.items.get((pk + "#LIVE", "META"))
            if (not meal or meal.get("source") or int((current or {}).get("count", 0)) != expected or
                    (pk + "#LIVE", chunk["sk"]) in self.items):
                raise Conflict
            self.items[(pk + "#LIVE", chunk["sk"])] = {"pk": pk + "#LIVE", **chunk}
            self.items[(pk + "#LIVE", "META")] = {"pk": pk + "#LIVE", **state}

    def read(self, pk: str) -> tuple[dict | None, dict | None]:
        with self.lock:
            meta = self.items.get((pk, "META"))
            pointer = self.items.get((pk, "ASSESSMENT#CURRENT"))
            return (meta.copy() if meta else None, pointer.copy() if pointer else None)

    def read_meta(self, pk: str) -> dict | None:
        with self.lock:
            meta = self.items.get((pk, "META"))
            return meta.copy() if meta else None

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

    def _advance_history(self, job_pk: str) -> None:
        key = (_history_pk(job_pk), "HISTORY#REVISION")
        revision = _history_number(self.items.get(key)) or 0
        if revision >= 10**18:
            raise ValueError("job history revision limit reached")
        self.items[key] = {"pk": key[0], "sk": key[1], "revision": revision + 1}

    def history_revision(self, subject: str) -> int | None:
        with self.lock:
            return _history_number(self.items.get((f"SCOPE#USER#{subject}#JOBS", "HISTORY#REVISION")))

    def create(self, pk: str, meta: dict) -> None:
        with self.lock:
            listing = _job_list_item(pk, meta)
            list_key = (listing["pk"], listing["sk"])
            if (pk, "META") in self.items or list_key in self.items:
                raise Conflict
            self._advance_history(pk)
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
            key = (listing["pk"], listing["sk"])
            if key not in self.items:
                self._advance_history(pk)
                self.items[key] = listing

    def bind_stream(self, pk: str, expected_version: int, meta: dict, stream_pk: str) -> None:
        with self.lock:
            current = self.items.get((pk, "META"))
            if not current or current["version"] != expected_version or (stream_pk, "BINDING") in self.items:
                raise Conflict
            self._advance_history(pk)
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
            self._advance_history(new_pk)
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
            if _history_relevant(event):
                self._advance_history(pk)
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

    def customer_restatement(self, pk: str, request_id: str) -> dict | None:
        item = self.table.get_item(Key={"pk": pk, "sk": f"RESTATEMENT#REQUEST#{request_id}"},
                                   ConsistentRead=True).get("Item")
        return _api_numbers(item)

    def create_customer_restatement(self, pk: str, expected_version: int, request: dict) -> dict:
        def existing_or_conflict():
            value = self.customer_restatement(pk, request["request_id"])
            if not value or any(value.get(field) != request.get(field) for field in
                                ("reason", "request_id", "job_version_at_detection", "source_stream_pk")):
                raise Conflict
            return value

        if self.customer_restatement(pk, request["request_id"]):
            return existing_or_conflict()
        try:
            self.client.transact_write_items(TransactItems=[
                {"ConditionCheck": {"TableName": self.table.name, "Key": self._item({"pk": pk, "sk": "META"}),
                    "ConditionExpression": "#v = :v", "ExpressionAttributeNames": {"#v": "version"},
                    "ExpressionAttributeValues": self._item({":v": expected_version})}},
                {"Put": {"TableName": self.table.name, "Item": self._item({"pk": pk, **request}),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed" for reason in
                   exc.response.get("CancellationReasons", [])):
                return existing_or_conflict()
            raise
        return {"pk": pk, **request}

    def create_meal(self, pk: str, records: list[dict]) -> None:
        operations = [{"Put": {"TableName": self.table.name,
                                "Item": self._item({"pk": pk, **row}),
                                "ConditionExpression": "attribute_not_exists(pk)"}}
                      for row in records]
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise Conflict from exc
            raise

    def read_meal(self, pk: str) -> list[dict]:
        rows = []
        start = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk), "ConsistentRead": True}
            if start:
                args["ExclusiveStartKey"] = start
            page = self.table.query(**args)
            rows.extend(page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                return _api_numbers(rows)

    def advance_meal(self, pk: str, expected_version: int, meta: dict,
                     step: dict, event: dict) -> None:
        operations = [
            {"Put": {"TableName": self.table.name, "Item": self._item({"pk": pk, **meta}),
                     "ConditionExpression": "#v = :expected",
                     "ExpressionAttributeNames": {"#v": "version"},
                     "ExpressionAttributeValues": {":expected": {"N": str(expected_version)}}}},
            {"Put": {"TableName": self.table.name, "Item": self._item({"pk": pk, **step}),
                     "ConditionExpression": "attribute_exists(pk)"}},
            {"Put": {"TableName": self.table.name, "Item": self._item({"pk": pk, **event}),
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise Conflict from exc
            raise

    def attach_meal_source(self, pk: str, expected_version: int, meta: dict,
                           event: dict, chunks: list[dict], live_count: int | None = None) -> None:
        operations = [
            {"Put": {"TableName": self.table.name, "Item": self._item({"pk": pk, **meta}),
                     "ConditionExpression": "#v = :expected", "ExpressionAttributeNames": {"#v": "version"},
                     "ExpressionAttributeValues": {":expected": {"N": str(expected_version)}}}},
            {"Put": {"TableName": self.table.name, "Item": self._item({"pk": pk, **event}),
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
        live_check = {"TableName": self.table.name, "Key": self._item({"pk": pk + "#LIVE", "sk": "META"}),
                      "ConditionExpression": "attribute_not_exists(pk)" if live_count is None else "#count = :expected"}
        if live_count is not None:
            live_check["ExpressionAttributeNames"] = {"#count": "count"}
            live_check["ExpressionAttributeValues"] = {":expected": {"N": str(live_count)}}
        operations.append({"ConditionCheck": live_check})
        operations.extend({"Put": {"TableName": self.table.name,
                                   "Item": self._item({"pk": pk + "#SOURCE", **row}),
                                   "ConditionExpression": "attribute_not_exists(pk)"}} for row in chunks)
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if _is_transaction_contention(exc):
                raise Conflict from exc
            raise

    def read_meal_source_chunk(self, pk: str, index: int) -> dict | None:
        row = self.table.get_item(Key={"pk": pk + "#SOURCE", "sk": f"RAW#{index:04d}"},
                                  ConsistentRead=True).get("Item")
        return _api_numbers(row)

    def read_meal_live(self, pk: str) -> dict | None:
        row = self.table.get_item(Key={"pk": pk + "#LIVE", "sk": "META"}, ConsistentRead=True).get("Item")
        return _api_numbers(row)

    def read_meal_live_chunk(self, pk: str, start: int) -> dict | None:
        row = self.table.get_item(Key={"pk": pk + "#LIVE", "sk": f"CHUNK#{start:06d}"}, ConsistentRead=True).get("Item")
        return _api_numbers(row)

    def read_meal_live_chunks(self, pk: str) -> list[dict]:
        items = []
        start_key = None
        while True:
            params = {"KeyConditionExpression": Key("pk").eq(pk + "#LIVE") & Key("sk").begins_with("CHUNK#"),
                      "ConsistentRead": True}
            if start_key:
                params["ExclusiveStartKey"] = start_key
            page = self.table.query(**params)
            items.extend(page.get("Items", []))
            if len(items) > 5000:
                raise ValueError("too many live chunks")
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                return _api_numbers(items)

    def append_meal_live(self, pk: str, expected: int, state: dict, chunk: dict) -> None:
        condition = "attribute_not_exists(pk)" if expected == 0 else "#count = :expected"
        live_put = {"TableName": self.table.name, "Item": self._item({"pk": pk + "#LIVE", **state}),
                    "ConditionExpression": condition}
        if expected:
            live_put["ExpressionAttributeNames"] = {"#count": "count"}
            live_put["ExpressionAttributeValues"] = {":expected": {"N": str(expected)}}
        operations = [
            {"ConditionCheck": {"TableName": self.table.name, "Key": self._item({"pk": pk, "sk": "META"}),
                                "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(#source)",
                                "ExpressionAttributeNames": {"#source": "source"}}},
            {"Put": live_put},
            {"Put": {"TableName": self.table.name, "Item": self._item({"pk": pk + "#LIVE", **chunk}),
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if _is_transaction_contention(exc):
                raise Conflict from exc
            raise

    def read(self, pk: str) -> tuple[dict | None, dict | None]:
        meta = self.table.get_item(Key={"pk": pk, "sk": "META"}, ConsistentRead=True).get("Item")
        pointer = self.table.get_item(
            Key={"pk": pk, "sk": "ASSESSMENT#CURRENT"}, ConsistentRead=True,
        ).get("Item")
        return _api_numbers(meta), _api_numbers(pointer)

    def read_meta(self, pk: str) -> dict | None:
        # Discovery reads several jobs in parallel. Low-level clients can be
        # shared between threads; boto3 resource objects cannot.
        item = self.client.get_item(TableName=self.table.name,
            Key={"pk": {"S": pk}, "sk": {"S": "META"}}, ConsistentRead=True).get("Item")
        if not item:
            return None
        decoder = TypeDeserializer()
        return _api_numbers({key: decoder.deserialize(value) for key, value in item.items()})

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

    def _history_transact(self, operations: list[dict]) -> None:
        # All history mutations contend on one per-owner version. Retry only
        # explicit transient transaction conflicts; never retry failed conditions.
        for attempt in range(4):
            try:
                self.client.transact_write_items(TransactItems=operations)
                return
            except self.client.exceptions.TransactionCanceledException as exc:
                reasons = exc.response.get("CancellationReasons", [])
                codes = {reason.get("Code") for reason in reasons}
                if attempt == 3 or "TransactionConflict" not in codes or \
                        not codes <= {None, "None", "TransactionConflict"}:
                    raise
                time.sleep(0.05 * (2 ** attempt) + random.uniform(0, 0.05))

    def _history_update(self, job_pk: str) -> dict:
        return {"Update": {"TableName": self.table.name,
            "Key": self._item({"pk": _history_pk(job_pk), "sk": "HISTORY#REVISION"}),
            "UpdateExpression": "ADD revision :one",
            "ConditionExpression": "attribute_not_exists(revision) OR revision < :max",
            "ExpressionAttributeValues": {":one": {"N": "1"}, ":max": {"N": "1000000000000000000"}}}}

    def save_validation_evidence(self, pk: str, expected_version: int, snapshot: dict,
                                 source_table: str, revision: int, receipt: dict) -> None:
        _evidence_snapshot_inputs(pk, expected_version, snapshot, source_table, revision, receipt)
        stream_pk = receipt["pk"]
        operations = [
            {"ConditionCheck": {"TableName": self.table.name, "Key": self._item({"pk": pk, "sk": "META"}),
                                "ConditionExpression": "#v = :v", "ExpressionAttributeNames": {"#v": "version"},
                                "ExpressionAttributeValues": {":v": {"N": str(expected_version)}}}},
            {"ConditionCheck": {"TableName": source_table,
                                "Key": self._item({"pk": stream_pk, "sk": "SOURCE#REVISION"}),
                                "ConditionExpression": "revision = :v",
                                "ExpressionAttributeValues": {":v": {"N": str(revision)}}}},
            {"ConditionCheck": {"TableName": source_table,
                                "Key": self._item({"pk": stream_pk, "sk": receipt["sk"]}),
                                "ConditionExpression": "receipt_version = :rv AND provenance = :p AND profile_id = :profile AND fixture_id = :fixture AND starting_cursor = :zero AND ending_cursor = :count AND record_count = :count AND ordered_raw_sha256 = :hash",
                                "ExpressionAttributeValues": self._item({":rv": "synthetic-jeyuk-collection-v1",
                                    ":p": "SYNTHETIC", ":profile": "mockprofile-110", ":fixture": stream_pk[6:],
                                    ":zero": 0, ":count": revision, ":hash": receipt["ordered_raw_sha256"]})}},
            {"Put": {"TableName": self.table.name, "Item": self._item({"pk": pk, **snapshot}),
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ]
        try:
            self._history_transact(operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if _is_transaction_contention(exc):
                raise Conflict from exc
            raise

    def validation_evidence_snapshots(self, pk: str) -> list[dict]:
        results, last_key = [], None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with("VALIDATION#EVIDENCE#"),
                    "ConsistentRead": True}
            if last_key:
                args["ExclusiveStartKey"] = last_key
            page = self.table.query(**args)
            results.extend(page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return _api_numbers(results)

    def history_revision(self, subject: str) -> int | None:
        item = self.table.get_item(Key={"pk": f"SCOPE#USER#{subject}#JOBS", "sk": "HISTORY#REVISION"},
                                   ConsistentRead=True).get("Item")
        return _history_number(item)

    def create(self, pk: str, meta: dict) -> None:
        listing = _job_list_item(pk, meta)
        try:
            self._history_transact([
                {"Put": {"TableName": self.table.name,
                         "Item": self._item({"pk": pk, "sk": "META", **meta}),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Put": {"TableName": self.table.name, "Item": self._item(listing),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                self._history_update(pk),
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            if _is_transaction_contention(exc):
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
            self._history_transact([
                {"Put": {"TableName": self.table.name, "Item": self._item(listing),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                self._history_update(pk),
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                existing = self.table.get_item(Key={"pk": listing["pk"], "sk": listing["sk"]},
                                               ConsistentRead=True).get("Item")
                if existing and existing.get("job_id") == meta["job_id"]:
                    return
            if _is_transaction_contention(exc):
                raise Conflict from exc
            raise

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
            self._history_update(pk),
        ]
        try:
            self._history_transact(operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if _is_transaction_contention(exc):
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
            self._history_update(new_pk),
        ]
        try:
            self._history_transact(operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if _is_transaction_contention(exc):
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
        if _history_relevant(event):
            operations.append(self._history_update(pk))
        try:
            self._history_transact(operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if _is_transaction_contention(exc):
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
