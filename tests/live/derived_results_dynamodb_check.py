"""Opt-in DynamoDB check for versioned comparison and selected-sample aggregates.

Run with AWS_PROFILE=foodtech and FOODTECH_COOKING_TABLE=foodtech-cooking-jobs-dev.
Only records under a random synthetic subject are created and removed.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import boto3
import httpx
from boto3.dynamodb.conditions import Attr, Key

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app


ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
TABLE = "foodtech-cooking-jobs-dev"
CSRF = {"X-CSRF-Token": "derived-validation-csrf"}


async def post(client: httpx.AsyncClient, path: str, body: dict | None = None, expected: int = 201) -> dict:
    response = await client.post(path, json=body if body is not None else {}, headers=CSRF)
    if response.status_code != expected:
        raise AssertionError(f"{path}: expected {expected}, got {response.status_code}: {response.text}")
    return response.json()


async def get(client: httpx.AsyncClient, path: str) -> dict:
    response = await client.get(path)
    if response.status_code != 200:
        raise AssertionError(f"{path}: expected 200, got {response.status_code}: {response.text}")
    return response.json()


def probe(item_id: str) -> dict:
    return {"item_id": item_id, "calibration_status": "OK", "samples": [
        {"at_second": second, "celsius": "75.0"} for second in range(61)
    ]}


async def completed_job(client: httpx.AsyncClient, subject: str, pks: list[str],
                        score: int, recipe: str) -> dict:
    job = await post(client, "/api/customer/cooking-jobs", {
        "planned_portions": 2, "recipe_version": recipe, "thickness_cm": "1.20",
    })
    path = f"/api/customer/cooking-jobs/{job['job_id']}"
    pks.append(cooking._pk(subject, job["job_id"]))
    placed_at = datetime.now(timezone.utc) - timedelta(seconds=300)
    for event in ("PREHEAT_COMPLETED", "COOKING_STARTED", "COOKING_COMPLETED"):
        timing = ({"actual_placed_at": placed_at.isoformat()} if event == "COOKING_STARTED"
                  else {"actual_lifted_at": (placed_at + timedelta(seconds=300)).isoformat()}
                  if event == "COOKING_COMPLETED" else {})
        job = await post(client, path + "/transitions",
                         {"event": event, "expected_version": job["version"], **timing}, 200)
    job = await post(client, path + "/safety/core-check", {
        "expected_version": job["version"], "probes": [probe("piece-1"), probe("piece-2")],
    }, 200)
    await post(client, path + "/assessments", {
        "expected_version": job["version"], "expected_current_version": 0,
        "kind": "INITIAL", "reason": None,
        "scores": {name: score for name in cooking.SCORE_ITEMS},
        "observations": {name: "SYNTHETIC storage validation" for name in cooking.SCORE_ITEMS},
        "scale_version": "tonkatsu-common-v1", "planned_portions": 2,
        "actual_portions": 2, "actual_mass_kg": "0.40", "load_percent": "50.00",
        "load_band": "PARTIAL",
    })
    return (await get(client, path))["job"]


def remove_synthetic_items(store: DynamoCookingStore, pks: list[str]) -> None:
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


def advance_version(store: DynamoCookingStore, pk: str) -> None:
    meta, _ = store.read(pk)
    assert meta is not None
    version = int(meta["version"])
    store.table.update_item(
        Key={"pk": pk, "sk": "META"},
        UpdateExpression="SET #version = :next",
        ConditionExpression=Attr("version").eq(Decimal(version)),
        ExpressionAttributeNames={"#version": "version"},
        ExpressionAttributeValues={":next": Decimal(version + 1)},
    )


async def verify() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use the foodtech profile and the exact development cooking table.")
    identity = boto3.Session(region_name=REGION).client("sts").get_caller_identity()
    if identity["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    store = DynamoCookingStore(TABLE)
    subject = "synthetic-derived-" + uuid.uuid4().hex
    current_subject = subject
    pks: list[str] = []
    summary = None

    async def session(_request, surface):
        assert surface == "customer"
        return Session("customer", current_subject, None, CSRF["X-CSRF-Token"], 9999999999, 0)

    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
             patch.object(cooking, "_session", side_effect=session), \
             patch.object(cooking, "get_cooking_store", return_value=store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                baseline = await completed_job(client, subject, pks, 3, "recipe-v1")
                candidate = await completed_job(client, subject, pks, 4, "recipe-v2")
                candidate_path = f"/api/customer/cooking-jobs/{candidate['job_id']}"
                comparison_path = candidate_path + "/comparisons"
                comparison = await post(client, comparison_path,
                                        {"baseline_job_id": baseline["job_id"]})
                comparison_record = comparison_path + "/" + comparison["comparison_id"]
                assert (await get(client, comparison_record + "/status"))["status"] == "CURRENT"

                period = datetime.now(timezone.utc)
                aggregate = await post(client, "/api/customer/assessment-aggregates", {
                    "job_ids": [baseline["job_id"], candidate["job_id"]],
                    "period_start": (period - timedelta(days=1)).isoformat(),
                    "period_end": (period + timedelta(days=1)).isoformat(),
                    "recipe_version": "recipe-v2", "load_band": "PARTIAL",
                })
                aggregate_pk = f"SCOPE#USER#{subject}#AGGREGATE#{aggregate['group_id']}"
                pks.append(aggregate_pk)
                aggregate_record = (f"/api/customer/assessment-aggregates/{aggregate['group_id']}"
                                    f"/results/{aggregate['result_id']}")
                assert aggregate["sample_count"] == 1
                assert (await get(client, aggregate_record + "/status"))["status"] == "CURRENT"

                candidate_pk = cooking._pk(subject, candidate["job_id"])
                restatement_sk = f"RESTATEMENT#RESULT#{uuid.uuid4().hex}"
                store.table.put_item(Item={"pk": candidate_pk, "sk": restatement_sk,
                                           "status": "DERIVED_ONLY", "provenance": "SYNTHETIC",
                                           "process_assessment_status": "NORMAL",
                                           "process_assessment": {"data_gap": False},
                                           "created_at": datetime.now(timezone.utc).isoformat(),
                                           "job_version_at_derivation": candidate["version"]})
                store.table.put_item(Item={"pk": candidate_pk, "sk": "PROCESS#LATEST_RESTATED",
                                           "version": 1, "result_sk": restatement_sk})
                assert (await get(client, aggregate_record + "/status"))["status"] == "STALE"
                restated_aggregate = await post(client, aggregate_record + "/refresh")
                assert restated_aggregate["result_id"] != aggregate["result_id"]
                assert restated_aggregate["mean_quality_score_percent"] == aggregate["mean_quality_score_percent"]
                candidate_ref = next(item for item in restated_aggregate["references"]
                                     if item["job_id"] == candidate["job_id"])
                assert candidate_ref["process_result_sk"] == restatement_sk
                assert (await get(client, aggregate_record))["result_id"] == aggregate["result_id"]

                candidate = (await get(client, candidate_path))["job"]
                revised = await post(client, candidate_path + "/assessments", {
                    "expected_version": candidate["version"], "expected_current_version": 1,
                    "kind": "REASSESSMENT", "reason": "SYNTHETIC reevaluation",
                    "scores": {name: 3 for name in cooking.SCORE_ITEMS},
                    "observations": {name: "SYNTHETIC revised observation" for name in cooking.SCORE_ITEMS},
                    "scale_version": "tonkatsu-common-v1", "planned_portions": 2,
                    "actual_portions": 2, "actual_mass_kg": "0.40", "load_percent": "50.00",
                    "load_band": "PARTIAL",
                })
                assert revised["version"] == 2
                assert (await get(client, comparison_record + "/status"))["status"] == "STALE"
                assert (await get(client, aggregate_record + "/status"))["status"] == "STALE"
                new_comparison = await post(client, comparison_record + "/refresh")
                new_aggregate = await post(client, aggregate_record + "/refresh")
                assert new_comparison["comparison_id"] != comparison["comparison_id"]
                assert new_aggregate["result_id"] != aggregate["result_id"]
                assert (await get(client, comparison_record))["score_total_delta"] == comparison["score_total_delta"]
                assert (await get(client, aggregate_record))["mean_quality_score_percent"] == \
                    aggregate["mean_quality_score_percent"]
                assert (await get(client, comparison_path + "/" + new_comparison["comparison_id"] + "/status")) \
                    ["status"] == "CURRENT"
                new_aggregate_record = (f"/api/customer/assessment-aggregates/{new_aggregate['group_id']}"
                                        f"/results/{new_aggregate['result_id']}")
                assert (await get(client, new_aggregate_record + "/status"))["status"] == "CURRENT"

                current_subject = "other-synthetic-user"
                assert (await client.get(comparison_record)).status_code == 404
                assert (await client.get(aggregate_record)).status_code == 404
                current_subject = subject

                baseline_pk = cooking._pk(subject, baseline["job_id"])
                candidate_pk = cooking._pk(subject, candidate["job_id"])
                original_save_comparison = store.save_comparison

                def compete_comparison(*args):
                    advance_version(store, baseline_pk)
                    return original_save_comparison(*args)

                with patch.object(store, "save_comparison", side_effect=compete_comparison):
                    conflict = await client.post(comparison_path, json={"baseline_job_id": baseline["job_id"]},
                                                 headers=CSRF)
                assert conflict.status_code == 409, conflict.text
                recovered_comparison = await post(client, comparison_path,
                                                  {"baseline_job_id": baseline["job_id"]})
                assert recovered_comparison["comparison_id"] != new_comparison["comparison_id"]

                original_save_aggregate = store.save_aggregate

                def compete_aggregate(*args):
                    advance_version(store, candidate_pk)
                    return original_save_aggregate(*args)

                with patch.object(store, "save_aggregate", side_effect=compete_aggregate):
                    conflict = await client.post("/api/customer/assessment-aggregates",
                                                 json={"job_ids": [baseline["job_id"], candidate["job_id"]],
                                                       "period_start": aggregate["selection"]["period_start"],
                                                       "period_end": aggregate["selection"]["period_end"],
                                                       "recipe_version": "recipe-v2", "load_band": "PARTIAL"},
                                                 headers=CSRF)
                assert conflict.status_code == 409, conflict.text
                recovered_aggregate = await post(client, aggregate_record + "/refresh")
                assert recovered_aggregate["result_id"] != new_aggregate["result_id"]
                assert (await get(client, aggregate_record))["result_id"] == aggregate["result_id"]

                summary = {"account": ACCOUNT, "region": REGION, "table": TABLE,
                           "comparison_stale_then_new_version": True,
                           "aggregate_stale_then_new_version": True,
                           "process_restatement_lineage_stale_then_new_version": True,
                           "historical_results_preserved": True,
                           "cross_subject_read_status": 404,
                           "concurrent_comparison_status": 409,
                           "concurrent_aggregate_status": 409,
                           "cleanup_partitions": len(pks)}
    finally:
        remove_synthetic_items(store, pks)
    assert summary is not None
    print(json.dumps(summary, ensure_ascii=False))


def main() -> None:
    asyncio.run(verify())


if __name__ == "__main__":
    main()
