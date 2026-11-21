"""Opt-in scoped evidence read against the existing development table."""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import boto3
import httpx

from foodtech_business_api import cooking
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app

TABLE = "foodtech-cooking-jobs-dev"


async def main():
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use the foodtech profile and development cooking table")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != "573693340100":
        raise SystemExit("Unexpected AWS account")
    table = session.resource("dynamodb").Table(TABLE)
    store = DynamoCookingStore(TABLE)
    subject, job_id = f"synthetic-evidence-{uuid4().hex}", str(uuid4())
    pk = cooking._pk(subject, job_id)
    result_sk = "RESTATEMENT#RESULT#synthetic"
    records = [
        {"pk": pk, "sk": "META", "job_id": job_id, "owner_subject": subject, "version": 7,
         "safety_version": 2, "status": "COMPLETED", "provenance": "SYNTHETIC", "menu": "TONKATSU",
         "recipe_version": "synthetic-v1", "equipment_profile": "synthetic-fryer", "thickness_profile": "STANDARD",
         "actual_cooking_seconds": 300, "safety_status": "PASSED", "evaluation_blocked": False,
         "process_assessment_status": "NORMAL", "private_note": "never-export"},
        {"pk": pk, "sk": "ASSESSMENT#CURRENT", "version": 1, "usable": True, "safety_version": 2,
         "assessment_sk": "ASSESSMENT#REV#000000000001", "quality_passed": True, "score_percent": "80"},
        {"pk": pk, "sk": "ASSESSMENT#REV#000000000001", "version": 1, "safety_version": 2, "status": "COMPLETED",
         "quality_passed": True, "score_percent": "80", "scale_version": "tonkatsu-common-v1"},
        {"pk": pk, "sk": "PROCESS#LATEST_RESTATED", "version": 1, "result_sk": result_sk},
        {"pk": pk, "sk": result_sk, "status": "DERIVED_ONLY", "job_version_at_derivation": 7,
         "process_assessment_status": "NORMAL", "process_assessment": {"rule_version": "synthetic"},
         "observation_coverage": {"sample_count": 2, "internal_gap_detected": False}},
    ]
    created = []
    try:
        for record in records:
            table.put_item(Item=record, ConditionExpression="attribute_not_exists(pk)")
            created.append({"pk": pk, "sk": record["sk"]})
        async def customer(_request, _surface): return SimpleNamespace(subject=subject)
        async def foreign(_request, _surface): return SimpleNamespace(subject="different-synthetic-user")
        with patch.object(cooking, "get_cooking_store", return_value=store), \
             patch.object(cooking, "_session", side_effect=customer):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/api/customer/cooking-jobs/{job_id}/recommendation-evidence"
                response = await client.get(path)
                response.raise_for_status()
                value = response.json()
                assert value["recorded"]["quality"]["score_percent"] == "80"
                assert value["observation_coverage"]["sample_count"] == 2
                assert value["completeness"] == "INSUFFICIENT" and not value["recommendation_execution_available"]
                assert "CORE_SERIES_MISSING_INVALID_OR_INCONSISTENT" in value["completeness_reasons"]
                assert "never-export" not in response.text and subject not in response.text
                group = "synthetic-group"
                def feed(name, start, end, group_id=None):
                    return {"ingredient": name, "started_at": f"2026-11-14T00:00:{start:02d}Z",
                            "completed_at": f"2026-11-14T00:00:{end:02d}Z", "addition_group_id": group_id,
                            "provenance": "SYNTHETIC"}
                table.put_item(Item={**records[0], "menu": "JEYUK", "jeyuk_feed_safety_aligned": True,
                    "actual_placed_at": "2026-11-14T00:00:05Z", "actual_lifted_at": "2026-11-14T00:02:00Z",
                    "jeyuk_addition_group_id": group, "jeyuk_additions_simultaneous": True,
                    "jeyuk_feeds": {"MEAT": feed("MEAT", 4, 5),
                                    "VEGETABLE": feed("VEGETABLE", 15, 16, group),
                                    "SAUCE": feed("SAUCE", 45, 46, group)}},
                    ConditionExpression="attribute_exists(pk)")
                jeyuk = await client.get(path)
                jeyuk.raise_for_status()
                timing = jeyuk.json()["recorded"]["jeyuk_feed_timing"]
                assert timing["status"] == "RECORDED" and timing["vegetable_start_offset_us"] == 10_000_000
                assert timing["sauce_start_offset_us"] == 40_000_000 and timing["simultaneous"] is True
                assert group not in jeyuk.text
                with patch.object(cooking, "_session", side_effect=foreign):
                    assert (await client.get(path)).status_code == 404
    finally:
        for key in reversed(created): table.delete_item(Key=key)
        if any(table.get_item(Key=key, ConsistentRead=True).get("Item") for key in created):
            raise RuntimeError("Test records remain")
    print(json.dumps({"owned_read": "PASS", "jeyuk_feed_timing": "PASS", "foreign_read": "404", "cleanup_records": len(created)}))


if __name__ == "__main__":
    asyncio.run(main())
