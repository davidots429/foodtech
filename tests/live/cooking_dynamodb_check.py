"""Opt-in live check for the synthetic cooking-job DynamoDB collection.

Run only with AWS_PROFILE=foodtech and FOODTECH_COOKING_TABLE=foodtech-cooking-jobs-dev.
The script creates synthetic items and removes exactly those items in finally.
"""

import asyncio
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

import boto3
import httpx
from boto3.dynamodb.conditions import Key

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore
from foodtech_business_api.main import app


ACCOUNT = "573693340100"
TABLE = "foodtech-cooking-jobs-dev"
SCORES = {name: 4 for name in cooking.SCORE_ITEMS}
OBSERVATIONS = {name: "SYNTHETIC live storage validation" for name in cooking.SCORE_ITEMS}


def probe(item_id: str, celsius: str = "75.0", missing_second: int | None = None) -> dict:
    return {"item_id": item_id, "calibration_status": "OK", "samples": [
        {"at_second": second, "celsius": celsius}
        for second in range(61) if second != missing_second
    ]}


def assessment(job: dict, current: int = 0, kind: str = "INITIAL", reason: str | None = None) -> dict:
    return {
        "expected_version": job["version"], "expected_current_version": current,
        "kind": kind, "reason": reason, "scores": SCORES, "observations": OBSERVATIONS,
        "scale_version": "tonkatsu-common-v1", "planned_portions": 2,
        "actual_portions": 2, "actual_mass_kg": "0.4", "load_percent": "50",
        "load_band": "PARTIAL",
    }


async def checked_post(client: httpx.AsyncClient, path: str, body: dict, expected: int = 200) -> dict:
    response = await client.post(path, json=body, headers={"X-CSRF-Token": "validation-csrf"})
    if response.status_code != expected:
        raise AssertionError(f"{path}: expected {expected}, got {response.status_code}: {response.text}")
    return response.json()


def remove_synthetic_items(store: DynamoCookingStore, pks: list[str]) -> None:
    with store.table.batch_writer() as writer:
        for pk in pks:
            query = store.table.query(KeyConditionExpression=Key("pk").eq(pk))
            while True:
                for item in query.get("Items", []):
                    writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                if "LastEvaluatedKey" not in query:
                    break
                query = store.table.query(
                    KeyConditionExpression=Key("pk").eq(pk),
                    ExclusiveStartKey=query["LastEvaluatedKey"],
                )


async def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use the foodtech profile and the exact development cooking table.")
    identity = boto3.Session(region_name="ap-southeast-2").client("sts").get_caller_identity()
    if identity["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    store = DynamoCookingStore(TABLE)
    subject = "synthetic-validation-" + uuid.uuid4().hex
    current_subject = subject
    created_pks: list[str] = []

    async def session(_request, surface):
        assert surface == "customer"
        return Session("customer", current_subject, None, "validation-csrf", 9999999999, 0)

    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
             patch.object(cooking, "_session", side_effect=session), \
             patch.object(cooking, "get_cooking_store", return_value=store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                job = await checked_post(client, "/api/customer/cooking-jobs",
                                         {"planned_portions": 2, "recipe_version": "synthetic-v1"}, 201)
                pk = cooking._pk(subject, job["job_id"])
                created_pks.append(pk)
                path = f"/api/customer/cooking-jobs/{job['job_id']}"
                current_subject = "other-synthetic-user"
                isolated = await client.get(path)
                assert isolated.status_code == 404
                current_subject = subject

                for event in ("PREHEAT_COMPLETED", "COOKING_STARTED", "COOKING_COMPLETED"):
                    job = await checked_post(client, path + "/transitions",
                                             {"event": event, "expected_version": job["version"]})

                incomplete = await checked_post(client, path + "/safety/core-check", {
                    "expected_version": job["version"],
                    "probes": [probe("piece-1", missing_second=30), probe("piece-2")],
                })
                assert incomplete["safety_status"] == "INCOMPLETE"
                job = await checked_post(client, path + "/safety/core-check", {
                    "expected_version": incomplete["version"],
                    "probes": [probe("piece-1"), probe("piece-2")],
                })
                assert job["safety_status"] == "PASSED"

                first = await checked_post(client, path + "/assessments", assessment(job), 201)
                assert first["version"] == 1 and first["quality_target"]
                stale = await client.post(path + "/assessments", json=assessment(job),
                                          headers={"X-CSRF-Token": "validation-csrf"})
                assert stale.status_code == 409

                snapshot = (await client.get(path)).json()
                job = await checked_post(client, path + "/safety/events", {
                    "event": "SUSPECTED", "expected_version": snapshot["job"]["version"],
                    "reason": "synthetic odor", "manual_stop_confirmed": True,
                })
                blocked = (await client.get(path)).json()
                assert blocked["current_assessment"]["version"] == 1, blocked["current_assessment"]
                assert not blocked["current_assessment"]["usable"]
                job = await checked_post(client, path + "/safety/events", {
                    "event": "CLEARED", "expected_version": job["version"],
                    "reason": "synthetic investigation cleared",
                })
                assert not (await client.get(path)).json()["current_assessment"]["usable"]
                job = await checked_post(client, path + "/safety/core-check", {
                    "expected_version": job["version"],
                    "probes": [probe("piece-1"), probe("piece-2")],
                })
                assert not (await client.get(path)).json()["current_assessment"]["usable"]
                second = await checked_post(client, path + "/assessments",
                                            assessment(job, 1, "REASSESSMENT", "synthetic recheck"), 201)
                assert second["version"] == 2 and second["previous_version"] == 1
                current = (await client.get(path)).json()
                assert current["current_assessment"]["version"] == 2, current["current_assessment"]
                assert current["current_assessment"]["usable"]
                revisions = (await client.get(path + "/assessments")).json()
                assert [item["version"] for item in revisions] == [1, 2], revisions

                recook = await checked_post(client, path + "/recook", {
                    "expected_version": current["job"]["version"], "reason": "synthetic recook check",
                }, 201)
                child_pk = cooking._pk(subject, recook["job_id"])
                created_pks.append(child_pk)
                assert recook["source_job_id"] == job["job_id"]
                assert recook["job_id"] != job["job_id"]

                parent_meta, parent_pointer = store.read(pk)
                expected = int(parent_meta["version"])
                previous_assessment = store.assessments(pk)[-1]

                def competing_write(marker: str) -> str:
                    changed = {**parent_meta, "version": Decimal(expected + 1), "validation_marker": marker}
                    next_assessment = {**previous_assessment,
                                       "sk": "ASSESSMENT#REV#000000000003", "version": 3,
                                       "previous_version": 2, "validation_marker": marker}
                    next_pointer = {**parent_pointer,
                                    "version": 3, "assessment_sk": next_assessment["sk"],
                                    "validation_marker": marker}
                    event = {"sk": f"EVENT#VALIDATION#{marker}", "occurred_at": cooking._now(),
                             "domain": "VALIDATION", "trigger": "COMPETING_WRITE"}
                    try:
                        store.commit(pk, expected, changed, next_assessment, next_pointer, event)
                        return "success"
                    except Conflict:
                        return "conflict"

                with ThreadPoolExecutor(max_workers=2) as executor:
                    outcomes = list(executor.map(competing_write, ("a", "b")))
                assert sorted(outcomes) == ["conflict", "success"], outcomes
                after, final_pointer = store.read(pk)
                assert int(after["version"]) == expected + 1
                events = store.events(pk)
                assert sum(item["domain"] == "VALIDATION" for item in events) == 1
                final_assessments = store.assessments(pk)
                assert [item["version"] for item in final_assessments] == [1, 2, 3]
                assert final_pointer["version"] == 3
                assert final_pointer["validation_marker"] == final_assessments[-1]["validation_marker"]

                print(json.dumps({
                    "account": ACCOUNT, "region": "ap-southeast-2", "table": TABLE,
                    "owner_isolation_status": isolated.status_code,
                    "missing_sample_status": incomplete["safety_status"],
                    "stale_assessment_status": stale.status_code,
                    "assessment_versions": [item["version"] for item in revisions],
                    "safety_blocked_old_pointer": True,
                    "recook_is_separate": True,
                    "concurrent_assessment_writes": outcomes,
                    "atomic_assessment_pointer_version": final_pointer["version"],
                    "validation_event_count": 1,
                    "cleanup_keys": created_pks,
                }, ensure_ascii=False))
    finally:
        remove_synthetic_items(store, created_pks)


if __name__ == "__main__":
    asyncio.run(main())
