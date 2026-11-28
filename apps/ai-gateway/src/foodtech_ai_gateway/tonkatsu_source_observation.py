"""Read fryer evidence using an explicitly scoped DynamoDB table resource.

A future authenticated execution must obtain the scoped resource and bind the job,
stream and revision before calling this reader. This reader issues no authority.
"""
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from foodtech_contracts.tonkatsu_prior_history import fryer_stream
from foodtech_contracts.collection_evidence import MAX_RECEIPT_RECORDS, matches_snapshot
from foodtech_contracts.tonkatsu_observed_inputs import observe_tonkatsu_batch
from foodtech_contracts.tonkatsu_source_quality import QUALITY_PREFIX, verify_source_quality
from foodtech_contracts.tonkatsu_safety_observation import MAX_SAFETY_EVENTS

RECEIPT_PREFIX = "COLLECTION#RECEIPT#"


def _read_rows(table, stream, prefix, limit, *, fields=()):
    paginator = table.meta.client.get_paginator("query")
    rows = []
    projection = {}
    if fields:
        names = {f"#field{index}": field for index, field in enumerate(fields)}
        projection = {"ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names}
    pages = paginator.paginate(
        TableName=table.name,
        KeyConditionExpression=Key("pk").eq(stream) & Key("sk").begins_with(prefix),
        ConsistentRead=True,
        PaginationConfig={"PageSize": min(limit + 1, 100)},
        **projection,
    )
    last_key = ""
    for page in pages:
        for row in page["Items"]:
            if row["pk"] != stream or not row["sk"].startswith(prefix) or row["sk"] <= last_key:
                raise ValueError("invalid fryer source page")
            last_key = row["sk"]
            rows.append(row)
        if len(rows) > limit:
            raise ValueError("fryer source row limit exceeded")
        cursor = page.get("LastEvaluatedKey")
        if cursor is None:
            return rows
        if not page["Items"] or cursor != {"pk": stream, "sk": last_key}:
            raise ValueError("invalid fryer source cursor")
    raise ValueError("fryer source pagination ended without completion")


def _revision_matches(table, stream, revision):
    item = table.get_item(Key={"pk": stream, "sk": "SOURCE#REVISION"}, ConsistentRead=True).get("Item")
    revision_value = item.get("revision") if item else None
    return (
        item is not None
        and item.get("pk") == stream
        and item.get("sk") == "SOURCE#REVISION"
        and type(revision_value) in (int, Decimal)
        and (type(revision_value) is int or revision_value.is_finite())
        and revision_value == revision
    )


def read_scoped_safety_events(cooking, job):
    return _read_rows(cooking, job["pk"], "EVENT#SAFETY#", MAX_SAFETY_EVENTS, fields=(
        "pk", "sk", "domain", "trigger", "from_status", "to_status", "probes",
        "segment", "reheat_segment_id", "occurred_at",
    ))


def observe_scoped_tonkatsu_source(*, table, job, scoped_stream, expected_revision, safety_events=None):
    """Read a fixed source revision with receipt and immutable quality evidence."""
    if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_RECEIPT_RECORDS:
        raise ValueError("bounded fryer source revision required")
    if fryer_stream(job.get("source_binding")) != scoped_stream:
        raise ValueError("bound fryer source required")
    if not _revision_matches(table, scoped_stream, expected_revision):
        raise ValueError("fryer source revision changed")
    raws = _read_rows(table, scoped_stream, "RAW#", expected_revision)
    receipts = _read_rows(table, scoped_stream, RECEIPT_PREFIX, 100)
    projections = _read_rows(table, scoped_stream, QUALITY_PREFIX, expected_revision)
    if len(raws) != expected_revision:
        raise ValueError("fryer source raw count changed")
    receipt = next((row for row in receipts if row.get("instance_id") == "mockprofile-100-a-001"
                    and matches_snapshot(row, scoped_stream, raws)), None)
    if receipt is None:
        raise ValueError("complete fryer collection evidence unavailable")
    verify_source_quality(scoped_stream, raws, receipt, projections)
    observed = observe_tonkatsu_batch(job, raws, safety_events=safety_events)
    if not _revision_matches(table, scoped_stream, expected_revision):
        raise ValueError("fryer source revision changed")
    return observed
