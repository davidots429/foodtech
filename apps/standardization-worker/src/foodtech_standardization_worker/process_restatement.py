"""Create an immutable process derivation from applied and late synthetic samples."""

import argparse
import hashlib
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
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
        requests = self._query(job_pk, "RESTATEMENT#REQUEST#")
        selected = [item for item in requests if item["status"] == "COMPLETED" or item["sk"] == request["sk"]]
        sources = {(item["source_assessment_pk"], item.get("source_assessment_sk", "ASSESSMENT#v1"))
                   for item in selected}
        events = self._query(job_pk, "EVENT#PROCESS#")
        sources.update((item["source_assessment_pk"], "ASSESSMENT#v1") for item in events)
        if len(sources) > MAX_RESTATEMENT_SAMPLES:
            raise ValueError("restatement sample limit exceeded")
        assessments = []
        for pk, sk in sorted(sources):
            item = self.data.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
            if not item:
                raise ValueError(f"restatement source missing: {pk}/{sk}")
            assessments.append(item)
        derived = replay_process(assessments, job)
        source_ids = derived["included_ingestion_event_ids"]
        digest = hashlib.sha256("\n".join(source_ids).encode("utf-8")).hexdigest()
        result_sk = f"RESTATEMENT#RESULT#{digest}"
        completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {"pk": job_pk, "sk": result_sk, "status": "DERIVED_ONLY",
                  "scope": "APPLIED_PROCESS_EVENTS_AND_COMPLETED_LATE_REQUESTS",
                  "trigger_request_sk": request["sk"], "job_id": job["job_id"],
                  "job_version_at_derivation": int(job["version"]),
                  "source_assessment_keys": [f"{pk}/{sk}" for pk, sk in sorted(sources)],
                  "created_at": completed_at, **derived}
        completed = {**request, "status": "COMPLETED", "result_sk": result_sk,
                     "completed_at": completed_at}
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
