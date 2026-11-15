"""Opt-in Jeyuk assessment check against the development cooking table."""

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


ACCOUNT = "573693340100"
TABLE = "foodtech-cooking-jobs-dev"
CSRF = {"X-CSRF-Token": "validation-csrf"}


async def post(client: httpx.AsyncClient, path: str, body: dict, expected: int = 200) -> dict:
    response = await client.post(path, json=body, headers=CSRF)
    if response.status_code != expected:
        raise AssertionError(f"{path}: {response.status_code}: {response.text}")
    return response.json()


def cleanup(store: DynamoCookingStore, partitions: list[str]) -> None:
    with store.table.batch_writer() as writer:
        for pk in partitions:
            args = {"KeyConditionExpression": Key("pk").eq(pk)}
            while True:
                page = store.table.query(**args)
                for item in page.get("Items", []):
                    writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                if "LastEvaluatedKey" not in page:
                    break
                args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def run() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use the foodtech profile and exact development cooking table.")
    identity = boto3.Session(region_name="ap-southeast-2").client("sts").get_caller_identity()
    if identity["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    store = DynamoCookingStore(TABLE)
    subject = "synthetic-jeyuk-assessment-" + uuid.uuid4().hex
    partitions = [f"SCOPE#USER#{subject}#JOBS"]

    async def session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)

    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
             patch.object(cooking, "_session", side_effect=session), \
             patch.object(cooking, "get_cooking_store", return_value=store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                job = await post(client, "/api/customer/cooking-jobs", {
                    "planned_portions": 2, "recipe_version": "synthetic-jeyuk-v1",
                    "equipment_profile": "jeyuk-small", "actual_load_kg": "1.8"}, 201)
                pk = cooking._pk(subject, job["job_id"])
                partitions.append(pk)
                path = f"/api/customer/cooking-jobs/{job['job_id']}"
                placed = datetime.now(timezone.utc) - timedelta(seconds=485)
                lifted = placed + timedelta(seconds=480)
                for event, timing in (("PREHEAT_COMPLETED", {}),
                                      ("COOKING_STARTED", {"actual_placed_at": placed.isoformat()}),
                                      ("COOKING_COMPLETED", {"actual_lifted_at": lifted.isoformat()})):
                    job = await post(client, path + "/transitions", {
                        "expected_version": job["version"], "event": event, **timing})
                old, _ = store.read(pk)
                store.commit(pk, int(old["version"]), {**old, "version": int(old["version"]) + 1,
                            "auto_safety_status": "PASSED", "mixture_hold_seconds": 60,
                            "safety_passed_at_second": 61})
                job = (await client.get(path)).json()["job"]
                samples = [{"phase": phase, "probe_id": f"synthetic-probe-{index}",
                            "meat_sample_id": f"synthetic-meat-{index}",
                            "thickest_or_clumped_part": True,
                            "sampled_at": (lifted + timedelta(seconds=index)).isoformat(),
                            "calibration_status": "OK", "celsius": "75"}
                           for index, phase in enumerate(("EARLY", "MIDDLE", "LATE"), 1)]
                job = await post(client, path + "/safety/discharge-check", {
                    "expected_version": job["version"], "kind": "INITIAL", "samples": samples})
                assert job["safety_status"] == "PASSED"
                scores = {item: 4 for item in cooking.SCORE_ITEMS}
                observations = {item: "SYNTHETIC Jeyuk observation" for item in cooking.SCORE_ITEMS}

                def assessment(version: int, current: int, kind: str, reason: str | None = None) -> dict:
                    return {"expected_version": version, "expected_current_version": current,
                            "kind": kind, "reason": reason, "scores": scores, "observations": observations,
                            "scale_version": "jeyuk-common-v1", "planned_portions": 2,
                            "actual_portions": 2, "actual_mass_kg": "1.8", "load_percent": "100",
                            "load_band": "REFERENCE", "quality_decision": "QUALITY_PASSED"}

                first = await post(client, path + "/assessments",
                                   assessment(job["version"], 0, "INITIAL"), 201)
                assert first["version"] == 1 and first["quality_target"]
                job = (await client.get(path)).json()["job"]
                job = await post(client, path + "/safety/events", {
                    "expected_version": job["version"], "event": "SUSPECTED",
                    "reason": "SYNTHETIC safety review"})
                snapshot = (await client.get(path)).json()
                assert not snapshot["current_assessment"]["usable"]
                job = await post(client, path + "/safety/events", {
                    "expected_version": job["version"], "event": "CLEARED",
                    "reason": "SYNTHETIC concern cleared"})
                job = await post(client, path + "/safety/discharge-check", {
                    "expected_version": job["version"], "kind": "REVALIDATION", "samples": samples})
                assert not (await client.get(path)).json()["current_assessment"]["usable"]
                second = await post(client, path + "/assessments",
                                    assessment(job["version"], 1, "REASSESSMENT", "SYNTHETIC recheck"), 201)
                snapshot = (await client.get(path)).json()
                assert second["version"] == 2 and snapshot["current_assessment"]["version"] == 2
                assert snapshot["current_assessment"]["usable"]
                assert [item["version"] for item in (await client.get(path + "/assessments")).json()] == [1, 2]
                print(json.dumps({"assessment_versions": [1, 2], "safety_blocked_old_pointer": True,
                                  "current_pointer": 2, "cleanup": "pending"}))
    finally:
        cleanup(store, partitions)
        print("synthetic cleanup complete")


if __name__ == "__main__":
    asyncio.run(run())
