"""Opt-in synthetic rice process, safety and reassessment against the development table."""
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import httpx

from foodtech_business_api import combi_meal, cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from combi_live_dynamodb_check import remove_and_verify

ACCOUNT = "573693340100"
TABLE = "foodtech-cooking-jobs-dev"


async def scenario(store, partitions):
    subject = "synthetic-rice-safety-" + uuid.uuid4().hex
    ids = [uuid.uuid4() for _ in range(4)]
    pk = f"SCOPE#USER#{subject}#COMBI_MEAL#{ids[0]}"
    partitions.append(pk)

    async def session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)

    headers = {"X-CSRF-Token": "validation-csrf"}
    with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
         patch.object(cooking, "_session", side_effect=session), \
         patch.object(cooking, "get_cooking_store", return_value=store):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            with patch.object(combi_meal, "uuid4", side_effect=ids):
                created = await client.post("/api/customer/combi-meals", headers=headers, json={
                    "equipment_size": "MEDIUM_FIXED_RACK_10", "rack_count": 1, "pan_count": 10,
                    "dry_rice_kg": "20", "drained_rice_kg": "24", "added_water_kg": "20",
                    "pan_tare_kg": "10", "rack_tare_kg": "0"})
            created.raise_for_status()
            assert created.json()["meal"]["meal_batch_id"] == str(ids[0])
            path = f"/api/customer/combi-meals/{ids[0]}"
            start = datetime.now(timezone.utc) - timedelta(seconds=1900)
            for version, event, offset in ((1, "RICE_STARTED", 0), (2, "RICE_PROGRAM_COMPLETED", 1800),
                                           (3, "RICE_UNLOADED", 1810)):
                response = await client.post(path + "/events", headers=headers, json={"expected_version": version,
                    "event": event, "observed_at": (start + timedelta(seconds=offset)).isoformat()})
                response.raise_for_status()
            samples = [{"second": index, "chamber_celsius": "100", "humidity_percent": "98",
                        "door": "CLOSED", "fan_level": 4} for index in range(1, 1801)]
            process = await client.post(path + "/rice-process", headers=headers,
                json={"expected_version": 4, "humidity_source": "MEASURED", "samples": samples})
            process.raise_for_status()
            assert process.json()["step"]["monitored_process_assessment"]["status"] == "NORMAL"
            check = await client.post(path + "/rice-safety", headers=headers, json={
                "expected_version": 5, "observation": "NO_ANOMALY", "note": "synthetic observation"})
            check.raise_for_status()
            assert check.json()["step"]["safety_status"] == "PASSED"
            quality = {"expected_version": 6, "expected_current_version": 0, "kind": "INITIAL",
                       "scores": {key: 4 for key in cooking.SCORE_ITEMS},
                       "observations": {key: "synthetic observation" for key in cooking.SCORE_ITEMS},
                       "quality_decision": "QUALITY_PASSED", "planned_portions": 3, "actual_portions": 3}
            first = await client.post(path + "/quality/rice", headers=headers, json=quality)
            first.raise_for_status()
            assert first.json()["step"]["quality_current"]["usable"]
            issue = await client.post(path + "/rice-safety", headers=headers, json={
                "expected_version": 7, "observation": "SUSPECTED", "note": "synthetic anomaly"})
            issue.raise_for_status()
            assert not issue.json()["step"]["quality_current"]["usable"]
            cleared = await client.post(path + "/rice-safety", headers=headers, json={
                "expected_version": 8, "observation": "NO_ANOMALY", "note": "synthetic resolved"})
            cleared.raise_for_status()
            assert not cleared.json()["step"]["quality_current"]["usable"]
            retry = await client.post(path + "/quality/rice", headers=headers, json={**quality,
                "expected_version": 9, "expected_current_version": 1, "kind": "REASSESSMENT",
                "reason": "synthetic reassessment"})
            retry.raise_for_status()
            assert retry.json()["step"]["quality_current"]["usable"]
            return {"process": "NORMAL", "safety": "PASSED", "quality_versions": 2,
                    "previous_pointer_blocked_until_reassessment": True}


async def main():
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use AWS_PROFILE=foodtech and the exact development cooking table")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    store = DynamoCookingStore(TABLE)
    partitions = []
    try:
        result = await scenario(store, partitions)
    finally:
        remove_and_verify(store.table, partitions)
    print(json.dumps({**result, "cleanup": "verified", "table": TABLE}))


if __name__ == "__main__":
    asyncio.run(main())
