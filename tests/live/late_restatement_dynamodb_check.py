"""Opt-in DynamoDB check for an immutable synthetic late-event derivation."""

import json
import os
import uuid
from unittest.mock import patch

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_contracts import stream_binding_key
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker
from foodtech_standardization_worker.process_restatement import DynamoProcessRestatement

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
COOKING_TABLE = "foodtech-cooking-jobs-dev"
DATA_TABLE = "foodtech-data-flow-dev"


def clean_partition(table, pk: str) -> None:
    with table.batch_writer() as writer:
        last = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk), "ConsistentRead": True}
            if last:
                args["ExclusiveStartKey"] = last
            page = table.query(**args)
            for item in page.get("Items", []):
                writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            last = page.get("LastEvaluatedKey")
            if not last:
                break
    assert table.query(KeyConditionExpression=Key("pk").eq(pk), ConsistentRead=True,
                       Select="COUNT")["Count"] == 0


def main() -> None:
    if (os.getenv("AWS_PROFILE") != "foodtech" or
            os.getenv("FOODTECH_COOKING_TABLE") != COOKING_TABLE or
            os.getenv("FOODTECH_DATA_TABLE") != DATA_TABLE):
        raise SystemExit("Use the foodtech profile and exact development tables.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    assert session.client("sts").get_caller_identity()["Account"] == ACCOUNT
    resource = session.resource("dynamodb")
    cooking, data = resource.Table(COOKING_TABLE), resource.Table(DATA_TABLE)
    run_id = uuid.uuid4().hex[:12]
    job_pk = f"SCOPE#USER#synthetic-restatement-{run_id}#JOB#test"
    event_pks = [f"EVENT#synthetic-restatement-{run_id}-{name}" for name in ("first", "late", "third")]
    binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
               "batch_id": f"batch-restatement-{run_id}"}
    stream_pk = stream_binding_key(**binding)
    job = {"pk": job_pk, "sk": "META", "job_id": "test", "version": 7,
           "status": "COMPLETED", "source_binding": binding,
           "process_assessment_status": "WARNING", "process_thickness_status": "NORMAL",
           "process_time_status": "NORMAL"}
    request_sk = f"RESTATEMENT#REQUEST#synthetic-restatement-{run_id}-late"
    try:
        cooking.put_item(Item=job)
        cooking.put_item(Item={"pk": stream_pk, "sk": "BINDING", "job_pk": job_pk})
        late_assessment = None
        for name, sequence, second, late in (("first", 1, 0, False), ("late", 2, 5, True),
                                             ("third", 3, 10, False)):
            event_id = f"synthetic-restatement-{run_id}-{name}"
            assessment = {"pk": f"EVENT#{event_id}", "sk": "ASSESSMENT#v1",
                          "source_pk": stream_pk, "ingestion_event_id": event_id, "dedup_status": "UNIQUE",
                          "quality_status": "WARNING" if late else "VALID",
                          "finding_codes": ["LATE_AFTER_GAP"] if late else [],
                          "normalized": {**binding, "sequence_number": sequence,
                                         "event_time": f"2026-11-14T00:00:{second:02d}Z",
                                         "device_status": "RUNNING",
                                         "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}}
            data.put_item(Item={"pk": stream_pk, "sk": f"RAW#{event_id}",
                                "ingestion_event_id": event_id})
            data.put_item(Item=assessment)
            if late:
                late_assessment = assessment
            if name == "first":
                cooking.put_item(Item={"pk": job_pk, "sk": f"EVENT#PROCESS#{event_id}",
                                       "source_assessment_pk": f"EVENT#{event_id}"})
        assert late_assessment is not None
        with patch.dict(os.environ, {"FOODTECH_COOKING_TABLE": COOKING_TABLE}):
            worker = DynamoStandardizationWorker(DATA_TABLE, "unused", "unused")
        assert worker._publish_process(late_assessment) == "RESTATEMENT_DERIVED"
        processor = DynamoProcessRestatement(COOKING_TABLE, DATA_TABLE)
        request = cooking.get_item(Key={"pk": job_pk, "sk": request_sk}, ConsistentRead=True)["Item"]
        result = cooking.get_item(Key={"pk": job_pk, "sk": request["result_sk"]},
                                  ConsistentRead=True)["Item"]
        assert request["status"] == "COMPLETED" and result["status"] == "DERIVED_ONLY"
        assert result["process_assessment_status"] == "NORMAL"
        assert result["process_assessment"]["data_gap"] is False
        assert len(result["included_ingestion_event_ids"]) == 3
        assert result["source_raw_count"] == 3
        assert result["scope"] == "STANDARDIZED_STREAM_SNAPSHOT"
        assert cooking.get_item(Key={"pk": job_pk, "sk": "META"}, ConsistentRead=True)["Item"] == job
        assert worker._publish_process(late_assessment) == "RESTATEMENT_DERIVED"
        assert processor.process(job_pk, f"synthetic-restatement-{run_id}-late") == "ALREADY_COMPLETED"
        print(json.dumps({"account": ACCOUNT, "region": REGION, "result": "DERIVED_ONLY",
                          "derived_status": result["process_assessment_status"],
                          "included_samples": len(result["included_ingestion_event_ids"]),
                          "job_unchanged": True, "replay": "ALREADY_COMPLETED",
                          "cleanup_partitions": 5}))
    finally:
        clean_partition(cooking, job_pk)
        clean_partition(cooking, stream_pk)
        for pk in event_pks:
            clean_partition(data, pk)
        clean_partition(data, stream_pk)


if __name__ == "__main__":
    main()
