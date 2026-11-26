"""Operator-only, read-only comparison of one customer's job keys and listing.

This is not mounted as an HTTP route. A DynamoDB Scan needs separate privileged
operator access and examines the table; the server-side filter and projection
return only the requested owner's job keys. The result cannot certify legacy
writers or become authorization for a model call.
"""
import argparse
import json
import os
import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import boto3
from boto3.dynamodb.conditions import Attr

from .cooking_store import DynamoCookingStore
from .subject_identity import valid_subject

MAX_LISTED = 100
DEFAULT_MAX_SCANNED = 10_000
PAGE_SIZE = 100


@dataclass(frozen=True)
class RosterObservation:
    owner: str = field(repr=False)
    revision: int
    roster_sha256: str = field(repr=False)
    proof_sha256: str = field(repr=False)
    count: int


def audit_snapshot(store, subject, *, max_scanned=DEFAULT_MAX_SCANNED):
    """Private audit observation, never a writer attestation or model permit."""
    observed = []
    result = audit(store, subject, max_scanned=max_scanned, _capture=observed.append)
    if result['status'] != 'MATCHED_IN_OBSERVATION' or len(observed) != 1:
        raise ValueError('complete tracked roster observation unavailable')
    return observed[0]


def audit(store: DynamoCookingStore, subject: str, *, max_scanned: int = DEFAULT_MAX_SCANNED, _capture=None) -> dict:
    if not valid_subject(subject):
        raise ValueError("invalid customer scope")
    if type(max_scanned) is not int or not 1 <= max_scanned <= DEFAULT_MAX_SCANNED:
        raise ValueError("invalid scan limit")
    if not re.fullmatch(r"foodtech-cooking-jobs-[a-z0-9-]{1,32}", store.table.name):
        raise ValueError("audit is restricted to a Foodtech cooking-jobs table")
    before = store.history_revision(subject)
    prefix = f"SCOPE#USER#{subject}#JOB#"
    paginator = store.table.meta.client.get_paginator("scan")
    pages = paginator.paginate(
        TableName=store.table.name, ConsistentRead=True,
        FilterExpression=Attr("pk").begins_with(prefix) & Attr("sk").eq("META"),
        ProjectionExpression="#pk, #sk", ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
        PaginationConfig={"PageSize": min(PAGE_SIZE, max_scanned)},
    )
    scanned, actual = 0, set()
    for page_number, page in enumerate(pages, 1):
        if page_number > (max_scanned + PAGE_SIZE - 1) // PAGE_SIZE:
            return {"status": "SCAN_LIMIT", "immediately_previous_verified": False}
        scanned += int(page.get("ScannedCount", 0))
        if scanned > max_scanned or (scanned >= max_scanned and page.get("LastEvaluatedKey")):
            return {"status": "SCAN_LIMIT", "immediately_previous_verified": False}
        for item in page.get("Items", []):
            key = item.get("pk")
            if not isinstance(key, str) or not key.startswith(prefix) or item.get("sk") != "META":
                return {"status": "INVALID_RECORD", "immediately_previous_verified": False}
            job_id = key[len(prefix):]
            try:
                if str(UUID(job_id)) != job_id or job_id in actual:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                return {"status": "INVALID_RECORD", "immediately_previous_verified": False}
            actual.add(job_id)
            if len(actual) > MAX_LISTED:
                return {"status": "HISTORY_LIMIT", "immediately_previous_verified": False}
    listed, cursor, seen = {}, None, set()
    while True:
        page = store.list_jobs(subject, 50, cursor)
        for item in page.get("items", []):
            job_id = item.get("job_id")
            try:
                if str(UUID(job_id)) != job_id or job_id in listed or not isinstance(item.get("created_at"), str):
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                return {"status": "INVALID_LISTING", "immediately_previous_verified": False}
            listed[job_id] = item["created_at"]
            if len(listed) > MAX_LISTED:
                return {"status": "HISTORY_LIMIT", "immediately_previous_verified": False}
        cursor = page.get("next_cursor")
        if not cursor:
            break
        if cursor in seen:
            return {"status": "INVALID_LISTING", "immediately_previous_verified": False}
        seen.add(cursor)
    missing, dangling = actual - listed.keys(), listed.keys() - actual
    inconsistent, metadata = 0, []
    for job_id in actual & listed.keys():
        item = store.read_meta(prefix + job_id)
        if not item or item.get("job_id") != job_id or item.get("owner_subject") != subject or \
                item.get("created_at") != listed[job_id] or type(item.get("version")) is not int or \
                not 1 <= item['version'] <= 10**18:
            inconsistent += 1
        else:
            metadata.append((job_id, item['version'], item['created_at']))
    after = store.history_revision(subject)
    if before != after:
        return {"status": "CHANGED_DURING_AUDIT", "immediately_previous_verified": False}
    status = ("MISMATCH" if missing or dangling or inconsistent else
              "UNTRACKED" if before is None else "MATCHED_IN_OBSERVATION")
    if _capture is not None and status == 'MATCHED_IN_OBSERVATION':
        if type(before) is not int or not 1 <= before <= 10**18 or any(
                datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() is None for value in listed.values()):
            raise ValueError('invalid tracked roster snapshot')
        roster = json.dumps(sorted(listed.items()), separators=(',', ':')).encode('ascii')
        proof = json.dumps({'owner': subject, 'revision': before, 'metadata': sorted(metadata)},
                           separators=(',', ':')).encode('ascii')
        _capture(RosterObservation(subject, before, hashlib.sha256(roster).hexdigest(),
                                   hashlib.sha256(proof).hexdigest(), len(actual)))
    return {"status": status, "record_count": len(actual), "listed_count": len(listed),
            "missing_count": len(missing), "dangling_count": len(dangling), "inconsistent_count": inconsistent,
            "history_revision": before, "immediately_previous_verified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only, operator-scoped cooking roster audit")
    parser.add_argument("--table", required=True)
    parser.add_argument("--region", default="ap-southeast-2")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--max-scanned", type=int, default=DEFAULT_MAX_SCANNED)
    args = parser.parse_args()
    subject = os.environ.get("FOODTECH_ROSTER_AUDIT_SUBJECT", "")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    store = DynamoCookingStore.__new__(DynamoCookingStore)
    store.table = session.resource("dynamodb").Table(args.table)
    store.client = session.client("dynamodb")
    result = audit(store, subject, max_scanned=args.max_scanned)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] == "MATCHED_IN_OBSERVATION" else 1


if __name__ == "__main__":
    raise SystemExit(main())
