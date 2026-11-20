"""Opt-in Jeyuk source audit against a unique synthetic development partition."""

import json
import os
import sys
import uuid
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_business_api.jeyuk_source_evidence import audit_source_records, read_source_records
from foodtech_contracts.jeyuk_process import RULE_VERSION as PROCESS_RULE_VERSION
from foodtech_standardization_worker.jeyuk_aws import DynamoJeyukProcessor


ACCOUNT = "573693340100"
TABLE = "foodtech-data-flow-dev"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/jeyuk-small-safety-v1.json"


def remove_partition(table, pk: str) -> None:
    with table.batch_writer() as writer:
        start_key = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk)}
            if start_key:
                args["ExclusiveStartKey"] = start_key
            page = table.query(**args)
            for item in page.get("Items", []):
                writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                break


def main() -> int:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for the development account.")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    table = session.resource("dynamodb").Table(TABLE)
    processor = DynamoJeyukProcessor(table, session.client("dynamodb"))
    batch_id = "jeyuk-evidence-" + uuid.uuid4().hex[:12]
    pk = f"JEYUK#{batch_id}"
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    second = 0
    try:
        for segment in fixture["samples"]:
            for _ in range(segment["seconds"]):
                second += 1
                raw = {"fixtureId": batch_id, "provenance": "SYNTHETIC",
                       "profileId": "mockprofile-110", "instanceId": "mockprofile-110-a-001",
                       "profile": "SMALL", "actualLoadKg": 1.8, "second": second,
                       "sampleIntervalSeconds": 1, "surfaceCelsius": float(segment["surfaceCelsius"]),
                       "actualRpm": float(segment["actualRpm"]),
                       "targetRpm": float(segment["targetRpm"]),
                       "motorLoadPercent": float(segment["motorLoadPercent"]),
                       "mixtureCelsius": float(segment["mixtureCelsius"]),
                       "calibrationStatus": segment["calibrationStatus"],
                       "safetySensorPosition": segment["safetySensorPosition"],
                       "lastIngredientAdditionCompleted": segment["lastIngredientAdditionCompleted"]}
                raw_sk = f"RAW#{second:06d}"
                table.put_item(Item={"pk": pk, "sk": raw_sk, "raw": json.dumps(raw)})
                processor.process({"profile_id": "mockprofile-110", "pk": pk,
                                   "fixture_id": batch_id, "raw_sk": raw_sk, "second": second})
        assessments, raws = read_source_records(TABLE, batch_id, second)
        last = assessments[-1]
        job = {"source_binding": {"batch_id": batch_id},
               "validation_context": {"actual_load_kg": "1.80"},
               "process_assessment": {"second": second, "source_assessment_sk": last["sk"],
                                      "status": last["process_status"],
                                      "rule_version": PROCESS_RULE_VERSION},
               "auto_safety_status": last["auto_safety_status"],
               "mixture_hold_seconds": last["mixture_hold_seconds"],
               "safety_passed_at_second": last["safety_passed_at_second"],
               "actual_cooking_seconds": second, "process_time_status": "NORMAL",
               "process_assessment_status": "NORMAL"}
        checks = audit_source_records(job, assessments, raws)
        if second != 61 or not all(checks.values()):
            raise AssertionError(f"source audit failed: {checks}")
        print(json.dumps({"seconds": second, "source_checks": checks,
                          "validation_activated": False}), flush=True)
        return 0
    finally:
        remove_partition(table, pk)
        if table.query(KeyConditionExpression=Key("pk").eq(pk), Select="COUNT")["Count"]:
            raise AssertionError("synthetic source records remain")
        print("synthetic cleanup complete", flush=True)


if __name__ == "__main__":
    sys.exit(main())
