"""Opt-in synthetic full batch on the existing development table."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import boto3
import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from combi_live_dynamodb_check import remove_and_verify
from cooking_dynamodb_check import probe, assessment, checked_post

TABLE = "foodtech-cooking-jobs-dev"


async def main():
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use the foodtech profile and exact development cooking table")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != "573693340100":
        raise SystemExit("Unexpected AWS account")
    store = DynamoCookingStore(TABLE)
    subject = "synthetic-full-batch-" + uuid4().hex
    partitions = [f"SCOPE#USER#{subject}#JOBS"]
    async def customer(_request, _surface):
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)
    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
             patch.object(cooking, "_session", side_effect=customer), \
             patch.object(cooking, "get_cooking_store", return_value=store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                job = await checked_post(client, "/api/customer/cooking-jobs", {"planned_portions": 2,
                    "recipe_version": "synthetic-v2", "thickness_cm": "1.20",
                    "equipment_profile": "synthetic-fryer-approved-v2"}, 201)
                partitions.append(cooking._pk(subject, job["job_id"]))
                path = f"/api/customer/cooking-jobs/{job['job_id']}"
                job = await checked_post(client, path + "/tonkatsu-batch", {"expected_version": job["version"],
                    "mode": "REFERENCE", "product_weights_g": [160] * 15})
                assert job["tonkatsu_batch"]["load_percent"] == "60.00"
                job = await checked_post(client, path + "/tonkatsu-initial-context", {
                    "expected_version": job["version"], "ingredient_temperature_c": "4.25",
                    "temperature_observed_at": (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat(),
                    "storage_state": "CHILLED", "oil_capacity_l": "15", "oil_batches_since_change": 0,
                    "oil_cooked_mass_kg": "0", "oil_changed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})
                assert job["tonkatsu_initial_context"]["status"] == "DECLARED_UNVERIFIED"
                evidence_response = await client.get(path + "/recommendation-evidence")
                evidence_response.raise_for_status()
                evidence = evidence_response.json()
                assert evidence["recorded"]["tonkatsu_initial_context"]["ingredient_temperature_c"] == "4.25"
                assert "FRYER_AND_BATCH_BASELINE" not in evidence["unverified_categories"]
                assert "INITIAL_CONTEXT_DECLARED_UNVERIFIED" in evidence["unverified_categories"]
                placed = datetime.now(timezone.utc) - timedelta(seconds=300)
                for event in ("PREHEAT_COMPLETED", "COOKING_STARTED", "COOKING_COMPLETED"):
                    timing = ({"actual_placed_at": placed.isoformat()} if event == "COOKING_STARTED" else
                              {"actual_lifted_at": (placed + timedelta(seconds=300)).isoformat()}
                              if event == "COOKING_COMPLETED" else {})
                    job = await checked_post(client, path + "/transitions",
                        {"event": event, "expected_version": job["version"], **timing})
                job = await checked_post(client, path + "/safety/core-check", {"expected_version": job["version"],
                    "probes": [probe(f"piece-{number}") for number in range(15)]})
                assert job["safety_status"] == "PASSED"
                body = {**assessment(job), "actual_mass_kg": "2.4", "load_percent": "60", "load_band": "REFERENCE"}
                result = await checked_post(client, path + "/assessments", body, 201)
                assert result["tonkatsu_batch_rule_version"] == "tonkatsu-batch-v2"
                assert result["quality_passed"]
    finally:
        remove_and_verify(store.table, partitions)
    print(json.dumps({"products": 15, "mass_kg": "2.4", "rated_load_percent": "60", "cleanup": "verified"}))


if __name__ == "__main__":
    asyncio.run(main())
