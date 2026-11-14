"""Create an immutable process derivation from a standardized stream snapshot."""

import argparse
import hashlib
import os
from datetime import datetime, timezone

import boto3
import rfc8785
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from foodtech_contracts import stream_binding_key
from foodtech_contracts.tonkatsu_process import process_status

from .process_rules import RULE_VERSION, advance

MAX_RESTATEMENT_SAMPLES = 1000


def replay_process(assessments: list[dict], job: dict) -> dict:
    """Re-evaluate the known process samples without changing the job's current state."""
    binding = job["source_binding"]

    def instant(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("restatement event time must include a timezone")
        return parsed

    placed = instant(job["actual_placed_at"]) if job.get("actual_placed_at") else None
    lifted = instant(job["actual_lifted_at"]) if job.get("actual_lifted_at") else None
    if any(not isinstance(item.get("normalized"), dict) for item in assessments):
        raise ValueError("restatement source has no normalized process sample")
    ordered = sorted(assessments, key=lambda item: (
        instant(item["normalized"]["event_time"]), int(item["normalized"]["sequence_number"]),
        item["ingestion_event_id"]))
    state = None
    last_sequence = None
    has_gap = False
    included = []
    for item in ordered:
        normalized = item["normalized"]
        if (item["dedup_status"] != "UNIQUE" or item["quality_status"] == "QUARANTINED" or
                normalized.get("device_status") != "RUNNING" or
                any(normalized.get(key) != value for key, value in binding.items())):
            raise ValueError("restatement source is not an eligible bound process sample")
        event_time = instant(normalized["event_time"])
        if (placed and event_time < placed) or (lifted and event_time > lifted):
            raise ValueError("restatement source is outside the cooking interval")
        sequence = int(normalized["sequence_number"])
        if last_sequence is not None and sequence <= last_sequence:
            raise ValueError("restatement sequence is repeated or reversed")
        # Original gap findings reflect arrival order; derive gaps from sequence and event time.
        state = advance(state, normalized, ingestion_event_id=item["ingestion_event_id"],
                        quality_status=item["quality_status"],
                        finding_codes={"DATA_GAP"} if last_sequence is not None and
                        sequence > last_sequence + 1 else set())
        if state is None:
            raise ValueError("restatement contains a repeated or reversed event time")
        has_gap = has_gap or state["data_gap"]
        included.append(item["ingestion_event_id"])
        last_sequence = sequence
    if not included:
        raise ValueError("restatement requires at least one process sample")
    status = "FAILED" if state["status"] == "FAILED" else process_status(
        state["temperature_status"], job.get("process_thickness_status", "NOT_MEASURED"),
        job.get("process_time_status", "NOT_MEASURED"), data_gap=has_gap)
    return {"process_assessment": state, "process_assessment_status": status,
            "has_unfilled_gap": has_gap, "rule_version": RULE_VERSION,
            "included_ingestion_event_ids": included}


class DynamoProcessRestatement:
    """Process pending requests; derived results never rewrite META or evaluation pointers."""

    def __init__(self, cooking_table: str, data_table: str, region: str = "ap-southeast-2"):
        session = boto3.Session(region_name=region)
        resource = session.resource("dynamodb")
        self.cooking = resource.Table(cooking_table)
        self.data = resource.Table(data_table)
        self.client = session.client("dynamodb")
        self.serializer = TypeSerializer()

    def _item(self, value: dict) -> dict:
        return {key: self.serializer.serialize(item) for key, item in value.items()}

    def _query(self, job_pk: str, prefix: str) -> list[dict]:
        items = []
        last = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(job_pk) & Key("sk").begins_with(prefix),
                    "ConsistentRead": True}
            if last:
                args["ExclusiveStartKey"] = last
            page = self.cooking.query(**args)
            items.extend(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last:
                return items

    def _stream_sources(self, stream_pk: str) -> list[dict]:
        items = []
        last = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(stream_pk) & Key("sk").begins_with("RAW#"),
                    "ConsistentRead": True}
            if last:
                args["ExclusiveStartKey"] = last
            page = self.data.query(**args)
            items.extend(page.get("Items", []))
            if len(items) > MAX_RESTATEMENT_SAMPLES:
                raise ValueError("restatement raw sample limit exceeded")
            last = page.get("LastEvaluatedKey")
            if not last:
                return items

    @staticmethod
    def _eligible(item: dict, job: dict) -> str | None:
        normalized = item.get("normalized")
        if item.get("dedup_status") != "UNIQUE":
            return "DUPLICATE_OR_CONFLICT"
        if item.get("quality_status") not in {"VALID", "WARNING"} or not isinstance(normalized, dict):
            return "QUALITY_NOT_ELIGIBLE"
        if normalized.get("device_status") != "RUNNING":
            return "DEVICE_NOT_RUNNING"
        if any(normalized.get(key) != value for key, value in job["source_binding"].items()):
            raise ValueError("restatement source binding mismatch")
        at = datetime.fromisoformat(normalized["event_time"].replace("Z", "+00:00"))
        placed = job.get("actual_placed_at")
        lifted = job.get("actual_lifted_at")
        if placed and at < datetime.fromisoformat(placed.replace("Z", "+00:00")):
            return "OUTSIDE_COOKING_INTERVAL"
        if lifted and at > datetime.fromisoformat(lifted.replace("Z", "+00:00")):
            return "OUTSIDE_COOKING_INTERVAL"
        return None

    def process(self, job_pk: str, ingestion_event_id: str) -> str:
        request_key = {"pk": job_pk, "sk": f"RESTATEMENT#REQUEST#{ingestion_event_id}"}
        request = self.cooking.get_item(Key=request_key, ConsistentRead=True).get("Item")
        if not request:
            return "REQUEST_NOT_FOUND"
        if request["status"] == "COMPLETED":
            return "ALREADY_COMPLETED"
        if request["status"] != "PENDING":
            return "REQUEST_NOT_PENDING"
        job = self.cooking.get_item(Key={"pk": job_pk, "sk": "META"},
                                    ConsistentRead=True).get("Item")
        if not job or job.get("source_binding") is None:
            return "JOB_NOT_BOUND"
        stream_pk = request.get("source_stream_pk")
        if not stream_pk:
            raise ValueError("restatement request has no source stream key")
        if stream_pk != stream_binding_key(**job["source_binding"]):
            raise ValueError("restatement request stream does not match job binding")
        requests = self._query(job_pk, "RESTATEMENT#REQUEST#")
        selected = [item for item in requests if item["status"] == "COMPLETED" or item["sk"] == request["sk"]]
        sources = {(item["source_assessment_pk"], item.get("source_assessment_sk", "ASSESSMENT#v1"))
                   for item in selected}
        events = self._query(job_pk, "EVENT#PROCESS#")
        sources.update((item["source_assessment_pk"], "ASSESSMENT#v1") for item in events)
        raw = self._stream_sources(stream_pk)
        raw_ids = {item["ingestion_event_id"] for item in raw}
        if ingestion_event_id not in raw_ids:
            raise RuntimeError("restatement trigger raw source is not ready")
        sources.update((f"EVENT#{item['ingestion_event_id']}", "ASSESSMENT#v1") for item in raw)
        if len(sources) > MAX_RESTATEMENT_SAMPLES:
            raise ValueError("restatement sample limit exceeded")
        assessments = []
        excluded = []
        for pk, sk in sorted(sources):
            item = self.data.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
            if not item:
                raise RuntimeError(f"restatement source assessment is not ready: {pk}/{sk}")
            if item.get("source_pk") != stream_pk:
                raise ValueError(f"restatement source stream mismatch: {pk}/{sk}")
            reason = self._eligible(item, job)
            if reason:
                excluded.append({"ingestion_event_id": item["ingestion_event_id"], "reason": reason})
            else:
                assessments.append(item)
        derived = replay_process(assessments, job)
        if {item["ingestion_event_id"] for item in self._stream_sources(stream_pk)} != raw_ids:
            return "SOURCE_CHANGED_RETRY"
        pointer_key = {"pk": job_pk, "sk": "PROCESS#LATEST_RESTATED"}
        previous_pointer = self.cooking.get_item(Key=pointer_key, ConsistentRead=True).get("Item")
        pointer_version = int(previous_pointer["version"]) + 1 if previous_pointer else 1
        manifest = {"request_sk": request["sk"], "job_version": int(job["version"]),
                    "source_assessment_keys": [f"{pk}/{sk}" for pk, sk in sorted(sources)],
                    "excluded": excluded}
        digest = hashlib.sha256(rfc8785.dumps(manifest)).hexdigest()
        result_sk = f"RESTATEMENT#RESULT#{digest}"
        completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {"pk": job_pk, "sk": result_sk, "status": "DERIVED_ONLY",
                  "scope": "STANDARDIZED_STREAM_SNAPSHOT",
                  "trigger_request_sk": request["sk"], "job_id": job["job_id"],
                  "job_version_at_derivation": int(job["version"]),
                  "source_stream_pk": stream_pk, "source_raw_count": len(raw),
                  "source_assessment_keys": manifest["source_assessment_keys"],
                  "excluded_sources": excluded, "input_hash": digest,
                  "created_at": completed_at, **derived}
        completed = {**request, "status": "COMPLETED", "result_sk": result_sk,
                     "completed_at": completed_at}
        pointer = {**pointer_key, "version": pointer_version, "result_sk": result_sk,
                   "updated_at": completed_at, "job_version_at_derivation": int(job["version"]),
                   "source_raw_count": len(raw)}
        pointer_condition = ({"ConditionExpression": "#v = :previous",
                              "ExpressionAttributeNames": {"#v": "version"},
                              "ExpressionAttributeValues": {":previous": {"N": str(previous_pointer["version"])}}}
                             if previous_pointer else {"ConditionExpression": "attribute_not_exists(pk)"})
        try:
            self.client.transact_write_items(TransactItems=[
                {"ConditionCheck": {"TableName": self.cooking.name,
                                    "Key": self._item({"pk": job_pk, "sk": "META"}),
                                    "ConditionExpression": "#v = :v",
                                    "ExpressionAttributeNames": {"#v": "version"},
                                    "ExpressionAttributeValues": {":v": {"N": str(job["version"])}}}},
                {"Put": {"TableName": self.cooking.name, "Item": self._item(result),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Put": {"TableName": self.cooking.name, "Item": self._item(completed),
                         "ConditionExpression": "#status = :pending",
                         "ExpressionAttributeNames": {"#status": "status"},
                         "ExpressionAttributeValues": {":pending": {"S": "PENDING"}}}},
                {"Put": {"TableName": self.cooking.name, "Item": self._item(pointer),
                         **pointer_condition}},
            ])
        except self.client.exceptions.TransactionCanceledException:
            current = self.cooking.get_item(Key=request_key, ConsistentRead=True).get("Item")
            if current and current["status"] == "COMPLETED":
                return "ALREADY_COMPLETED"
            return "STALE_RETRY"
        return "DERIVED"


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive a result for one pending late event")
    parser.add_argument("job_pk")
    parser.add_argument("ingestion_event_id")
    args = parser.parse_args()
    cooking_table = os.environ["FOODTECH_COOKING_TABLE"]
    data_table = os.environ["FOODTECH_DATA_TABLE"]
    print(DynamoProcessRestatement(cooking_table, data_table).process(
        args.job_pk, args.ingestion_event_id))


if __name__ == "__main__":
    main()
