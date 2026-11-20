"""Opt-in DynamoDB check for an immutable synthetic late-event derivation."""

import json
import asyncio
import os
import uuid
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_contracts import stream_binding_key
from foodtech_contracts.collection_evidence import CompletionTracker
from foodtech_collection_adapter.aws_flow import DynamoOrderedIngestor
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.source_revision import _read as read_source_revision
from foodtech_business_api import recommendation_evidence
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
    event_pks = [f"EVENT#synthetic-restatement-{run_id}-{name}"
                 for name in ("first", "late", "third", "followup", "race")]
    binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
               "batch_id": f"batch-restatement-{run_id}"}
    stream_pk = stream_binding_key(**binding)
    job = {"pk": job_pk, "sk": "META", "job_id": "test", "version": 7,
           "status": "COMPLETED", "source_binding": binding,
           "actual_placed_at": "2026-11-13T23:59:58Z", "actual_lifted_at": "2026-11-14T00:00:20Z",
           "process_assessment_status": "WARNING", "process_thickness_status": "NORMAL",
           "process_time_status": "NORMAL"}
    request_sk = f"RESTATEMENT#REQUEST#synthetic-restatement-{run_id}-late"
    try:
        cooking.put_item(Item=job)
        cooking.put_item(Item={"pk": stream_pk, "sk": "BINDING", "job_pk": job_pk})
        late_assessment = None
        tracker = CompletionTracker(0, "mockprofile-100", binding["device_id"])
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
            source_record = {"sourceId": binding["source_id"], "deviceId": binding["device_id"],
                             "batchId": binding["batch_id"], "sequenceNumber": sequence}
            raw = json.dumps(source_record, separators=(",", ":"))
            tracker.record(raw, source_record)
            data.put_item(Item={"pk": stream_pk, "sk": f"RAW#{event_id}",
                                "ingestion_event_id": event_id, "raw": raw, "provenance": "SYNTHETIC"})
            data.put_item(Item=assessment)
            if late:
                late_assessment = assessment
            if name == "first":
                cooking.put_item(Item={"pk": job_pk, "sk": f"EVENT#PROCESS#{event_id}",
                                       "source_assessment_pk": f"EVENT#{event_id}"})
        assert late_assessment is not None
        receipt = tracker.finish(next_cursor=3, complete=True, completion_explicit=True)
        collector = DynamoOrderedIngestor.__new__(DynamoOrderedIngestor)
        collector.table = data
        collector.save_completion_receipt(receipt, "synthetic-live-check")
        data.put_item(Item={"pk": stream_pk, "sk": "SOURCE#REVISION", "revision": 3})
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
        assert result["source_revision"] == 3
        assert read_source_revision(DATA_TABLE, stream_pk) == 3
        assert result["collection_evidence"]["raw_snapshot_matches"] is True
        assert result["observation_coverage"]["before_first_us"] == 2_000_000
        assert result["observation_coverage"]["after_last_us"] == 10_000_000
        assert result["observation_coverage"]["internal_gap_detected"] is False
        assert result["scope"] == "STANDARDIZED_STREAM_SNAPSHOT"
        latest = cooking.get_item(Key={"pk": job_pk, "sk": "PROCESS#LATEST_RESTATED"},
                                  ConsistentRead=True)["Item"]
        assert latest["result_sk"] == result["sk"] and latest["version"] == 1
        assert DynamoCookingStore(COOKING_TABLE).latest_process_result(job_pk)["sk"] == result["sk"]
        customer_store = DynamoCookingStore(COOKING_TABLE)
        read_job = AsyncMock(return_value=(None, customer_store, job_pk, job, None))
        with patch.object(recommendation_evidence, "_read", read_job):
            evidence = asyncio.run(recommendation_evidence.read_evidence(None, "test"))
            assert evidence["source_revision_status"] == "MATCHED_AT_READ"
        followup_id = f"synthetic-restatement-{run_id}-followup"
        followup = {"pk": f"EVENT#{followup_id}", "sk": "ASSESSMENT#v1",
                    "source_pk": stream_pk, "ingestion_event_id": followup_id,
                    "dedup_status": "UNIQUE", "quality_status": "VALID", "finding_codes": [],
                    "normalized": {**binding, "sequence_number": 4,
                                   "event_time": "2026-11-14T00:00:15Z",
                                   "device_status": "RUNNING",
                                   "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}}
        data.put_item(Item={"pk": stream_pk, "sk": f"RAW#{followup_id}",
                            "ingestion_event_id": followup_id})
        data.put_item(Item=followup)
        data.update_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"},
                         UpdateExpression="ADD #r :one", ExpressionAttributeNames={"#r": "revision"},
                         ExpressionAttributeValues={":one": 1})
        with patch.object(recommendation_evidence, "_read", read_job):
            evidence = asyncio.run(recommendation_evidence.read_evidence(None, "test"))
            assert evidence["source_revision_status"] == "CHANGED"
        assert worker._publish_process(followup) == "RESTATEMENT_DERIVED"
        followup_request = cooking.get_item(
            Key={"pk": job_pk, "sk": f"RESTATEMENT#REQUEST#{followup_id}"},
            ConsistentRead=True)["Item"]
        assert followup_request["reason"] == "FOLLOWUP_SAMPLE"
        next_result = cooking.get_item(Key={"pk": job_pk, "sk": followup_request["result_sk"]},
                                       ConsistentRead=True)["Item"]
        latest = cooking.get_item(Key={"pk": job_pk, "sk": "PROCESS#LATEST_RESTATED"},
                                  ConsistentRead=True)["Item"]
        assert latest["version"] == 2 and latest["result_sk"] == next_result["sk"]
        assert len(next_result["included_ingestion_event_ids"]) == 4
        assert next_result["collection_evidence"]["raw_snapshot_matches"] is False
        assert next_result["source_revision"] == 4
        assert read_source_revision(DATA_TABLE, stream_pk) == 4
        assert next_result["observation_coverage"]["after_last_us"] == 5_000_000
        assert cooking.get_item(Key={"pk": job_pk, "sk": result["sk"]},
                                ConsistentRead=True)["Item"] == result
        assert cooking.get_item(Key={"pk": job_pk, "sk": "META"}, ConsistentRead=True)["Item"] == job
        assert worker._publish_process(late_assessment) == "RESTATEMENT_DERIVED"
        assert processor.process(job_pk, f"synthetic-restatement-{run_id}-late") == "ALREADY_COMPLETED"
        race_id = f"synthetic-restatement-{run_id}-race"
        race = {**followup, "pk": f"EVENT#{race_id}", "ingestion_event_id": race_id,
                "normalized": {**followup["normalized"], "sequence_number": 5,
                               "event_time": "2026-11-14T00:00:20Z"}}
        data.put_item(Item={"pk": stream_pk, "sk": f"RAW#{race_id}", "ingestion_event_id": race_id})
        data.put_item(Item=race)
        data.update_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"},
                         UpdateExpression="ADD #r :one", ExpressionAttributeNames={"#r": "revision"},
                         ExpressionAttributeValues={":one": 1})
        cooking.put_item(Item={"pk": job_pk, "sk": f"RESTATEMENT#REQUEST#{race_id}", "status": "PENDING",
                               "source_assessment_pk": race["pk"], "source_stream_pk": stream_pk})
        original_client = processor.client
        def racing_transaction(**kwargs):
            data.update_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"},
                             UpdateExpression="ADD #r :one", ExpressionAttributeNames={"#r": "revision"},
                             ExpressionAttributeValues={":one": 1})
            return original_client.transact_write_items(**kwargs)
        processor.client = SimpleNamespace(exceptions=original_client.exceptions,
                                           transact_write_items=racing_transaction)
        assert processor.process(job_pk, race_id) == "STALE_RETRY"
        assert cooking.get_item(Key={"pk": job_pk, "sk": "PROCESS#LATEST_RESTATED"},
                                ConsistentRead=True)["Item"]["version"] == 2
        print(json.dumps({"account": ACCOUNT, "region": REGION, "result": "DERIVED_ONLY",
                          "derived_status": result["process_assessment_status"],
                          "included_samples": len(next_result["included_ingestion_event_ids"]),
                          "latest_version": int(latest["version"]),
                          "job_unchanged": True, "replay": "ALREADY_COMPLETED",
                          "source_revision_race": "STALE_RETRY", "customer_revision_status": [
                              "MATCHED_AT_READ", "CHANGED"], "cleanup_partitions": 8}))
    finally:
        clean_partition(cooking, job_pk)
        clean_partition(cooking, stream_pk)
        for pk in event_pks:
            clean_partition(data, pk)
        clean_partition(data, stream_pk)


if __name__ == "__main__":
    main()
