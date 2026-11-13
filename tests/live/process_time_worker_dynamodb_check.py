"""Opt-in live check of synthetic temperature and cooking-time decisions.

Uses only the existing foodtech development cooking table. Random synthetic job
and binding partitions are deleted in finally.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import httpx
from boto3.dynamodb.conditions import Key

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from foodtech_contracts import stream_binding_key
from foodtech_standardization_worker.process_bridge import DynamoProcessBridge


ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
TABLE = "foodtech-cooking-jobs-dev"
CSRF = {"X-CSRF-Token": "process-time-validation-csrf"}


async def post(client: httpx.AsyncClient, path: str, body: dict, expected: int = 200) -> dict:
    response = await client.post(path, json=body, headers=CSRF)
    if response.status_code != expected:
        raise AssertionError(f"{path}: expected {expected}, got {response.status_code}: {response.text}")
    return response.json()


async def job_snapshot(client: httpx.AsyncClient, path: str) -> dict:
    response = await client.get(path)
    if response.status_code != 200:
        raise AssertionError(f"{path}: expected 200, got {response.status_code}: {response.text}")
    return response.json()["job"]


def process_sample(binding: dict, event_id: str, event_at: datetime) -> dict:
    return {"pk": f"EVENT#{event_id}", "ingestion_event_id": event_id,
            "dedup_status": "UNIQUE", "quality_status": "VALID", "finding_codes": [],
            "normalized": {**binding, "device_status": "RUNNING", "event_time": event_at.isoformat(),
                           "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}}


async def started_job(client: httpx.AsyncClient, subject: str, pks: list[str],
                      run_id: str, suffix: str) -> tuple[dict, str, dict, datetime]:
    job = await post(client, "/api/customer/cooking-jobs", {
        "planned_portions": 2, "recipe_version": "synthetic-time-worker-v1", "thickness_cm": "1.20",
    }, 201)
    job_pk = cooking._pk(subject, job["job_id"])
    pks.append(job_pk)
    path = f"/api/customer/cooking-jobs/{job['job_id']}"
    binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
               "batch_id": f"batch-a-time-worker-{run_id}-{suffix}"}
    pks.append(stream_binding_key(**binding))
    job = await post(client, path + "/source-binding", {
        "expected_version": job["version"], **binding,
    })
    job = await post(client, path + "/transitions", {
        "event": "PREHEAT_COMPLETED", "expected_version": job["version"],
    })
    placed_at = datetime.now(timezone.utc) - timedelta(seconds=310)
    job = await post(client, path + "/transitions", {
        "event": "COOKING_STARTED", "expected_version": job["version"],
        "actual_placed_at": placed_at.isoformat(),
    })
    return job, path, binding, placed_at


def remove_partitions(store: DynamoCookingStore, pks: list[str]) -> None:
    with store.table.batch_writer() as writer:
        for pk in pks:
            last_key = None
            while True:
                args = {"KeyConditionExpression": Key("pk").eq(pk), "ConsistentRead": True}
                if last_key:
                    args["ExclusiveStartKey"] = last_key
                page = store.table.query(**args)
                for item in page.get("Items", []):
                    writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                last_key = page.get("LastEvaluatedKey")
                if not last_key:
                    break
    for pk in pks:
        remaining = store.table.query(KeyConditionExpression=Key("pk").eq(pk),
                                      ConsistentRead=True, Select="COUNT")
        if remaining["Count"]:
            raise AssertionError(f"Synthetic records remain in partition {pk}")


async def verify() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use the foodtech profile and exact development cooking table.")
    identity = boto3.Session(profile_name="foodtech", region_name=REGION).client("sts").get_caller_identity()
    if identity["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    store = DynamoCookingStore(TABLE)
    bridge = DynamoProcessBridge(TABLE)
    run_id = uuid.uuid4().hex[:12]
    subject = f"synthetic-time-worker-{run_id}"
    pks: list[str] = []
    summary = None

    async def session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, CSRF["X-CSRF-Token"], 9999999999, 0)

    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
             patch.object(cooking, "_session", side_effect=session), \
             patch.object(cooking, "get_cooking_store", return_value=store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                _, path, binding, placed_at = await started_job(client, subject, pks, run_id, "normal")
                first = process_sample(binding, f"{run_id}-normal-1", placed_at + timedelta(seconds=5))
                second = process_sample(binding, f"{run_id}-normal-2", placed_at + timedelta(seconds=10))
                assert bridge.apply(first) == "INSUFFICIENT_DATA"
                before = await job_snapshot(client, path)
                original_commit = store.commit

                def competing_worker(*args):
                    assert bridge.apply(second) == "INSUFFICIENT_DATA"
                    return original_commit(*args)

                complete = {"event": "COOKING_COMPLETED", "expected_version": before["version"],
                            "actual_lifted_at": (placed_at + timedelta(seconds=300)).isoformat()}
                with patch.object(store, "commit", side_effect=competing_worker):
                    conflict = await client.post(path + "/transitions", json=complete, headers=CSRF)
                assert conflict.status_code == 409, conflict.text
                after_worker = await job_snapshot(client, path)
                assert after_worker["status"] == "RUNNING"
                complete["expected_version"] = after_worker["version"]
                completed = await post(client, path + "/transitions", complete)
                assert completed["status"] == "COMPLETED"
                assert completed["actual_cooking_seconds"] == 300
                assert completed["process_temperature_status"] == "NORMAL"
                assert completed["process_time_status"] == "NORMAL"
                assert completed["process_assessment_status"] == "NORMAL"
                assert bridge.apply(second) == "REPLAYED"

                _, failed_path, failed_binding, failed_placed_at = await started_job(
                    client, subject, pks, run_id, "early")
                failed_sample = process_sample(failed_binding, f"{run_id}-early-1",
                                               failed_placed_at + timedelta(seconds=5))
                assert bridge.apply(failed_sample) == "INSUFFICIENT_DATA"
                failed_before = await job_snapshot(client, failed_path)
                failed = await post(client, failed_path + "/transitions", {
                    "event": "COOKING_COMPLETED", "expected_version": failed_before["version"],
                    "actual_lifted_at": (failed_placed_at + timedelta(seconds=260)).isoformat(),
                })
                assert failed["status"] == "FAILED" and failed["evaluation_blocked"]
                assert failed["process_temperature_status"] == "NORMAL"
                assert failed["process_time_status"] == "EARLY_REMOVAL"
                assert failed["process_assessment_status"] == "FAILED"
                late_sample = process_sample(failed_binding, f"{run_id}-early-2",
                                             failed_placed_at + timedelta(seconds=10))
                assert bridge.apply(late_sample) == "JOB_NOT_ACTIVE"
                events = await client.get(failed_path + "/events")
                assert events.status_code == 200
                assert any(event["trigger"] == "COOKING_COMPLETED" and
                           event["process_time_status"] == "EARLY_REMOVAL" for event in events.json())
                summary = {"account": ACCOUNT, "region": REGION, "table": TABLE,
                           "normal_combined_status": completed["process_assessment_status"],
                           "competing_worker_api_status": conflict.status_code,
                           "retry_cooking_seconds": completed["actual_cooking_seconds"],
                           "time_failure_status": failed["process_time_status"],
                           "post_completion_worker_status": "JOB_NOT_ACTIVE",
                           "cleanup_partitions": len(pks)}
    finally:
        remove_partitions(store, pks)
    assert summary is not None
    print(json.dumps(summary, ensure_ascii=False))


def main() -> None:
    asyncio.run(verify())


if __name__ == "__main__":
    main()
