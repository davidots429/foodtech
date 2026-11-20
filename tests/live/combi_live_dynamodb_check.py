"""Opt-in synthetic customer live-source and transaction-race validation on DynamoDB.

Run with AWS_PROFILE=foodtech FOODTECH_COOKING_TABLE=foodtech-cooking-jobs-dev.
Uses the existing development table only; removes and checks the random test partitions.
The HTTP authentication in this experiment is a synthetic local session.
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

from foodtech_business_api import combi_meal, cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
TABLE = "foodtech-cooking-jobs-dev"


def records_for(meal):
    meal_id = meal["meal_batch_id"]
    phases = (("RICE", meal["rice_cooking_job_id"]), ("TRANSITION", meal["mode_transition_id"]),
              ("MACKEREL", meal["mackerel_cooking_job_id"]))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for index, (phase, job) in enumerate(phases, 1):
        at = (start + timedelta(seconds=index - 1)).isoformat()
        records.append(json.dumps({"eventId": f"{meal_id}-{index:06d}", "mealBatchId": meal_id,
            "cookingJobId": job, "deviceId": "synthetic-combi-validation", "mockInstanceId": "mockprofile-120-a-001",
            "eventType": "OBSERVATION", "occurredAt": at, "receivedAt": at, "sequence": index,
            "schemaVersion": "synthetic-combi-v1", "programVersion": "v1", "provenance": "SYNTHETIC",
            "dataQuality": "VALID", "phase": phase, "phaseSecond": 1, "chamberCelsius": 100,
            "climateSetpointPercent": 100, "measuredHumidityPercent": 98,
            "humidityValueSource": "MEASURED", "fanLevel": 4, "doorState": "CLOSED"}, separators=(",", ":")))
    return records


async def run_scenario(store, subject, created_partitions):
    current_subject = subject
    async def session(_request, surface):
        if surface != "customer":
            raise AssertionError("unexpected session surface")
        return Session("customer", current_subject, None, "validation-csrf", 9999999999, 0)
    headers = {"X-CSRF-Token": "validation-csrf"}
    with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
         patch.object(cooking, "_session", side_effect=session), \
         patch.object(cooking, "get_cooking_store", return_value=store):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            ids = [uuid.uuid4() for _ in range(4)]
            pk = f"SCOPE#USER#{subject}#COMBI_MEAL#{ids[0]}"
            created_partitions.extend((pk, pk + "#LIVE", pk + "#SOURCE"))
            with patch.object(combi_meal, "uuid4", side_effect=ids):
                response = await client.post("/api/customer/combi-meals", headers=headers, json={
                "equipment_size": "MEDIUM_FIXED_RACK_10", "rack_count": 1, "pan_count": 10,
                "dry_rice_kg": "20", "drained_rice_kg": "24", "added_water_kg": "20",
                    "pan_tare_kg": "10", "rack_tare_kg": "0"})
            response.raise_for_status()
            meal = response.json()["meal"]
            meal_id = meal["meal_batch_id"]
            if meal_id != str(ids[0]):
                raise AssertionError("Unexpected synthetic meal ID")
            path = f"/api/customer/combi-meals/{meal_id}"
            records = records_for(meal)
            variant = json.loads(records[0]); variant["chamberCelsius"] = 101
            variant = json.dumps(variant, separators=(",", ":"))
            def batch(record):
                return {"device_id": "synthetic-combi-validation", "expected_cursor": 0, "records": [record]}
            responses = await asyncio.gather(*(client.post(path + "/source-live", headers=headers, json=batch(raw))
                                               for raw in (records[0], variant)))
            statuses = sorted(response.status_code for response in responses)
            assert statuses == [200, 409], statuses
            winner = responses[0].status_code == 200
            if not winner:
                records[0] = variant
            duplicate = await client.post(path + "/source-live", headers=headers, json=batch(records[0]))
            assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
            current_subject = subject + "-other"
            isolated = await client.get(path + "/source-live")
            assert isolated.status_code == 404
            current_subject = subject
            live = await client.get(path + "/source-live")
            assert live.json()["count"] == 1 and live.json()["state"] == "COLLECTING"
            rest = await client.post(path + "/source-live", headers=headers, json={
                "device_id": "synthetic-combi-validation", "expected_cursor": 1, "records": records[1:]})
            assert rest.status_code == 200 and rest.json()["next_cursor"] == 3, rest.text
            completion = await client.post(path + "/source-observations", headers=headers, json={
                "expected_version": 1, "device_id": "synthetic-combi-validation", "records": records})
            assert completion.status_code == 201, completion.text
            assert (await client.get(path + "/source-live")).json()["state"] == "COMPLETED"
            saved = await client.get(path + "/source-observations")
            assert saved.status_code == 200 and saved.json()["records"] == records
            return {"competing_appends": statuses, "idempotent_replay": True,
                    "customer_isolation": isolated.status_code, "final_record_count": 3}


def remove_and_verify(table, partitions):
    for pk in partitions:
        items, start = [], None
        while True:
            params = {"KeyConditionExpression": Key("pk").eq(pk), "ConsistentRead": True}
            if start:
                params["ExclusiveStartKey"] = start
            page = table.query(**params)
            items.extend(page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                break
        with table.batch_writer() as writer:
            for item in items:
                writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
        remaining = table.query(KeyConditionExpression=Key("pk").eq(pk), ConsistentRead=True, Limit=1)
        if remaining.get("Items"):
            raise RuntimeError("Synthetic validation partition cleanup failed")


async def main():
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use AWS_PROFILE=foodtech and the exact development cooking table")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    store = DynamoCookingStore(TABLE)
    partitions = []
    try:
        result = await run_scenario(store, "synthetic-combi-live-" + uuid.uuid4().hex, partitions)
    finally:
        remove_and_verify(store.table, partitions)
    print(json.dumps({**result, "cleanup": "verified", "table": TABLE, "region": REGION}))


if __name__ == "__main__":
    asyncio.run(main())
