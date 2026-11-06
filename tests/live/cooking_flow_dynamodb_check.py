"""Opt-in live check of time failures and a competing process update.

Uses only the existing development cooking table. Every created synthetic
partition and stream binding is removed even when an assertion fails.
"""

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import boto3
import httpx
from boto3.dynamodb.conditions import Key

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from foodtech_contracts import stream_binding_key
from foodtech_standardization_worker.process import assess
from foodtech_standardization_worker.process_bridge import DynamoProcessBridge


ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
TABLE = "foodtech-cooking-jobs-dev"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/tonkatsu-process-high-v1.json"
HEADERS = {"X-CSRF-Token": "validation-csrf"}


async def post(client: httpx.AsyncClient, path: str, body: dict, expected: int = 200) -> dict:
    response = await client.post(path, json=body, headers=HEADERS)
    if response.status_code != expected:
        raise AssertionError(f"{path}: expected {expected}, got {response.status_code}: {response.text}")
    return response.json()


def remove_partition(table, pk: str) -> None:
    with table.batch_writer() as writer:
        last_key = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk), "ConsistentRead": True}
            if last_key:
                args["ExclusiveStartKey"] = last_key
            page = table.query(**args)
            for item in page.get("Items", []):
                writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return


async def create_running(client, subject: str, created: list[str], placed_at: datetime) -> tuple[dict, str]:
    job = await post(client, "/api/customer/cooking-jobs", {
        "planned_portions": 2, "recipe_version": "synthetic-flow-validation-v1",
        "thickness_cm": "1.20",
    }, 201)
    created.append(cooking._pk(subject, job["job_id"]))
    path = f"/api/customer/cooking-jobs/{job['job_id']}"
    job = await post(client, path + "/transitions", {
        "event": "PREHEAT_COMPLETED", "expected_version": job["version"],
    })
    job = await post(client, path + "/transitions", {
        "event": "COOKING_STARTED", "expected_version": job["version"],
        "actual_placed_at": placed_at.isoformat(),
    })
    return job, path


async def check_time_failure(client, subject: str, created: list[str], seconds: int,
                             expected_status: str) -> dict:
    placed_at = datetime.now(timezone.utc) - timedelta(seconds=420)
    job, path = await create_running(client, subject, created, placed_at)
    failed = await post(client, path + "/transitions", {
        "event": "COOKING_COMPLETED", "expected_version": job["version"],
        "actual_lifted_at": (placed_at + timedelta(seconds=seconds)).isoformat(),
    })
    assert failed["status"] == "FAILED" and failed["evaluation_blocked"]
    assert failed["actual_cooking_seconds"] == seconds
    assert failed["process_time_status"] == expected_status
    assert failed["process_assessment_status"] == "FAILED"
    assert failed["completion_recording_delay_seconds"] >= 0
    events = (await client.get(path + "/events")).json()
    assert events[-1]["process_time_status"] == expected_status
    blocked = await client.post(path + "/assessments", headers=HEADERS,
                                json={"expected_version": failed["version"],
                                      "expected_current_version": 0, "kind": "INITIAL",
                                      "scores": {name: 4 for name in cooking.SCORE_ITEMS},
                                      "observations": {name: "synthetic" for name in cooking.SCORE_ITEMS},
                                      "planned_portions": 2, "actual_portions": 2,
                                      "actual_mass_kg": "0.4", "load_percent": "50",
                                      "load_band": "PARTIAL"})
    assert blocked.status_code == 409, blocked.text
    return {"time_status": expected_status, "job_status": failed["status"],
            "recorded_seconds": failed["actual_cooking_seconds"]}


async def check_competing_update(client, subject: str, store: DynamoCookingStore,
                                 created: list[str], bindings: list[str]) -> dict:
    placed_at = datetime.now(timezone.utc) - timedelta(seconds=300)
    batch_id = f"batch-a-flow-{uuid.uuid4().hex[:12]}"
    unbound = await post(client, "/api/customer/cooking-jobs", {
        "planned_portions": 2, "recipe_version": "synthetic-race-validation-v1",
        "thickness_cm": "1.20",
    }, 201)
    created.append(cooking._pk(subject, unbound["job_id"]))
    race_path = f"/api/customer/cooking-jobs/{unbound['job_id']}"
    binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
               "batch_id": batch_id}
    bindings.append(stream_binding_key(**binding))
    unbound = await post(client, race_path + "/source-binding", {
        "expected_version": unbound["version"], **binding,
    })
    unbound = await post(client, race_path + "/transitions", {
        "event": "PREHEAT_COMPLETED", "expected_version": unbound["version"],
    })
    unbound = await post(client, race_path + "/transitions", {
        "event": "COOKING_STARTED", "expected_version": unbound["version"],
        "actual_placed_at": placed_at.isoformat(),
    })
    record = json.loads(FIXTURE.read_text())["records"][0]
    record["batchId"] = batch_id
    record["recordId"] = f"rec-a-flow-{uuid.uuid4().hex[:12]}"
    event_id = f"synthetic-flow-{uuid.uuid4().hex}"
    standardized = assess(event_id, json.dumps(record))
    assert standardized.normalized and standardized.quality_status != "QUARANTINED"
    worker_input = {"pk": f"EVENT#{event_id}", "ingestion_event_id": event_id,
                    "dedup_status": "UNIQUE", "quality_status": standardized.quality_status,
                    "finding_codes": [finding.code for finding in standardized.findings],
                    "normalized": standardized.normalized}
    bridge = DynamoProcessBridge(TABLE)
    original_commit = store.commit
    outcomes = []

    def competing_commit(*args):
        if args[-1] and args[-1].get("trigger") == "COOKING_COMPLETED":
            worker = threading.Thread(target=lambda: outcomes.append(bridge.apply(worker_input)))
            worker.start()
            worker.join(timeout=15)
            assert not worker.is_alive(), "worker process assessment timed out"
        return original_commit(*args)

    with patch.object(store, "commit", side_effect=competing_commit):
        response = await client.post(race_path + "/transitions", headers=HEADERS, json={
            "event": "COOKING_COMPLETED", "expected_version": unbound["version"],
            "actual_lifted_at": (placed_at + timedelta(seconds=300)).isoformat(),
        })
    assert response.status_code == 409, response.text
    assert outcomes == ["WARNING"], outcomes
    current = (await client.get(race_path)).json()["job"]
    assert current["status"] == "RUNNING" and current["process_temperature_status"] == "HIGH_WARNING"
    completed = await post(client, race_path + "/transitions", {
        "event": "COOKING_COMPLETED", "expected_version": current["version"],
        "actual_lifted_at": (placed_at + timedelta(seconds=300)).isoformat(),
    })
    assert completed["status"] == "COMPLETED"
    assert completed["process_assessment_status"] == "WARNING"
    assert bridge.apply(worker_input) == "REPLAYED"
    return {"stale_completion_http": response.status_code,
            "worker_result": outcomes[0], "retry_process_status": completed["process_assessment_status"]}


async def check_safety_interruption(client, subject: str, store: DynamoCookingStore,
                                    created: list[str]) -> dict:
    placed_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    job, path = await create_running(client, subject, created, placed_at)
    held = await post(client, path + "/safety/events", {
        "event": "SUSPECTED", "expected_version": job["version"],
        "reason": "synthetic discoloration", "partial_scores": {"ingredient_condition": 4},
        "partial_observations": {"ingredient_condition": "synthetic pre-cook check"},
    })
    assert held["status"] == "SAFETY_HOLD" and held["evaluation_status"] == "INTERRUPTED"
    assert held["manual_stop_confirmed_at"] is None
    blocked = await client.post(path + "/assessments", headers=HEADERS, json={
        "expected_version": held["version"], "expected_current_version": 0, "kind": "INITIAL",
        "scores": {name: 4 for name in cooking.SCORE_ITEMS},
        "observations": {name: "synthetic" for name in cooking.SCORE_ITEMS},
        "planned_portions": 2, "actual_portions": 2, "actual_mass_kg": "0.4",
        "load_percent": "50", "load_band": "PARTIAL",
    })
    assert blocked.status_code == 409, blocked.text
    premature_clear = await client.post(path + "/safety/events", headers=HEADERS, json={
        "event": "CLEARED", "expected_version": held["version"], "reason": "synthetic check",
    })
    assert premature_clear.status_code == 409
    reviewed = await post(client, path + "/safety/events", {
        "event": "UNDER_REVIEW", "expected_version": held["version"],
        "reason": "operator manually stopped synthetic device", "manual_stop_confirmed": True,
        "manual_stopped_at": datetime.now(timezone.utc).isoformat(),
    })
    assert reviewed["manual_stop_confirmed_at"] is not None
    cleared = await post(client, path + "/safety/events", {
        "event": "CLEARED", "expected_version": reviewed["version"],
        "reason": "synthetic concern excluded",
    })
    assert cleared["status"] == "SAFETY_HOLD"
    resumed = await post(client, path + "/transitions", {
        "event": "COOKING_RESUMED", "expected_version": cleared["version"],
        "reason": "operator chose to resume",
    })
    assert resumed["status"] == "RUNNING"
    events = store.events(cooking._pk(subject, job["job_id"]))
    suspected = [item for item in events if item.get("trigger") == "SUSPECTED"]
    assert len(suspected) == 1
    assert suspected[0]["partial_scores"] == {"ingredient_condition": 4}
    assert suspected[0]["partial_observations"] == {"ingredient_condition": "synthetic pre-cook check"}
    assert store.assessments(cooking._pk(subject, job["job_id"])) == []
    return {"hold_status": held["status"], "assessment_block_http": blocked.status_code,
            "premature_clear_http": premature_clear.status_code, "resumed_status": resumed["status"]}


async def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use the foodtech profile and exact development cooking table.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    store = DynamoCookingStore(TABLE)
    subject = "synthetic-flow-validation-" + uuid.uuid4().hex
    created: list[str] = []
    bindings: list[str] = []

    async def fake_session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)

    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
             patch.object(cooking, "_session", side_effect=fake_session), \
             patch.object(cooking, "get_cooking_store", return_value=store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://local") as client:
                early = await check_time_failure(client, subject, created, 269, "EARLY_REMOVAL")
                maximum = await check_time_failure(client, subject, created, 361, "MAX_TIME_EXCEEDED")
                race = await check_competing_update(client, subject, store, created, bindings)
                safety = await check_safety_interruption(client, subject, store, created)
                print(json.dumps({"account": ACCOUNT, "region": REGION, "table": TABLE,
                                  "early": early, "maximum": maximum, "race": race,
                                  "safety": safety}), flush=True)
    finally:
        cleanup_errors = []
        for pk in created + bindings:
            try:
                remove_partition(store.table, pk)
            except Exception as exc:
                cleanup_errors.append(f"{pk}: {exc}")
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))
        print("synthetic cleanup complete", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
