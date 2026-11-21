"""Opt-in scoped prior safety evidence against the existing development table."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import uuid
from unittest.mock import patch

import boto3
import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from process_flow_dynamodb_check import ACCOUNT, COOKING_TABLE, REGION, remove_partition


async def run() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for the development account.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    table = session.resource("dynamodb").Table(COOKING_TABLE)
    store = DynamoCookingStore(COOKING_TABLE)
    subject = "prior-test-" + uuid.uuid4().hex[:12]
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    keys = [cooking._pk(subject, value) for value in ids]
    listing_pk = f"SCOPE#USER#{subject}#JOBS"
    async def fake_session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "test-csrf", 9999999999, 0)
    try:
        for index, (job_id, pk) in enumerate(zip(ids, keys)):
            failed = index == 0
            meta = {"job_id": job_id, "owner_subject": subject, "version": 1, "safety_version": 1,
                    "status": "FAILED" if failed else "COMPLETED", "menu": "JEYUK",
                    "provenance": "SYNTHETIC", "equipment_profile": "jeyuk-small",
                    "recipe_version": "synthetic-v1", "planned_portions": 1,
                    "created_at": f"2026-01-0{index + 1}T00:00:00Z",
                    "actual_placed_at": f"2026-01-0{index + 1}T00:00:00Z",
                    "actual_lifted_at": f"2026-01-0{index + 1}T00:08:00Z",
                    "actual_cooking_seconds": 480, "safety_status": "FAILED" if failed else "PASSED",
                    "evaluation_blocked": True, "process_assessment_status": "FAILED" if failed else "NORMAL",
                    "source_binding": {"source_id": "synthetic-rest", "device_id": "mockprofile-110-a-001",
                                       "batch_id": f"jeyuk-test-{uuid.uuid4().hex[:12]}"}}
            await asyncio.to_thread(store.create, pk, meta)
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false", "FOODTECH_DATA_TABLE": ""}), \
                patch.object(cooking, "_session", side_effect=fake_session), \
                patch.object(cooking, "get_cooking_store", return_value=store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                discovery = await client.get(f"/api/customer/cooking-jobs/{ids[1]}/prior-job-candidate")
                discovery.raise_for_status()
                candidate = discovery.json()
                if (candidate["status"], candidate["candidate"]["job_id"],
                        candidate["immediately_previous_verified"]) != ("CANDIDATE", ids[0], False):
                    raise AssertionError("Unexpected prior job candidate")
                result = await client.get(f"/api/customer/cooking-jobs/{ids[1]}/recommendation-evidence",
                                          params={"prior_job_id": ids[0]})
                result.raise_for_status()
                prior = result.json()["selected_prior_job"]
                if (prior["safety_status"], prior["order_status"], prior["immediately_previous_verified"],
                        prior["collection"]["status"]) != ("FAILED", "ENDED_BEFORE", False, "NOT_CONFIGURED"):
                    raise AssertionError("Unexpected prior safety evidence")
                if result.json()["recommendation_execution_available"]:
                    raise AssertionError("Unverified prior must not enable recommendation execution")
        if store.history_revision(subject) != 2:
            raise AssertionError("Job creation did not advance history revision")
        prior_meta = store.read_meta(keys[0])
        store.ensure_listed(keys[0], prior_meta)
        if store.history_revision(subject) != 2:
            raise AssertionError("Idempotent listing changed history revision")
        listing_key = {"pk": listing_pk, "sk": f"CREATED#{prior_meta['created_at']}#{ids[0]}"}
        table.delete_item(Key=listing_key)
        store.ensure_listed(keys[0], prior_meta)
        if store.history_revision(subject) != 3:
            raise AssertionError("Restoring a missing listing did not advance history revision")
        new_id = str(uuid.uuid4()); new_pk = cooking._pk(subject, new_id); keys.append(new_pk)
        extra = {**{key: value for key, value in prior_meta.items() if key not in {"pk", "sk"}},
                 "job_id": new_id, "version": 1, "created_at": "2026-01-03T00:00:00Z",
                 "status": "READY", "actual_placed_at": None, "source_binding": None}
        store.create(new_pk, extra)
        stream_pk = f"JEYUK#jeyuk-history-{uuid.uuid4().hex[:12]}"; keys.append(stream_pk)
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-110-a-001", "batch_id": stream_pk[6:]}
        bound = {**extra, "version": 2, "source_binding": binding}
        store.bind_stream(new_pk, 1, bound, stream_pk)
        started = {**bound, "version": 3, "status": "RUNNING", "actual_placed_at": "2026-01-03T00:00:00Z"}
        store.commit(new_pk, 2, started, event={"sk": "EVENT#test-start", "trigger": "COOKING_STARTED"})
        recook_id = str(uuid.uuid4()); recook_pk = cooking._pk(subject, recook_id); keys.append(recook_pk)
        recook = {**extra, "job_id": recook_id, "created_at": "2026-01-04T00:00:00Z"}
        store.create_recook(new_pk, 3, recook_pk, recook)
        if store.history_revision(subject) != 7:
            raise AssertionError("Create, bind, start or recook history revision was not atomic")
        concurrent = []
        for index in range(4):
            item_id = str(uuid.uuid4()); item_pk = cooking._pk(subject, item_id); keys.append(item_pk)
            item = {**extra, "job_id": item_id, "created_at": f"2026-01-{index + 5:02d}T00:00:00Z"}
            concurrent.append((item_pk, item))
        def create_one(args):
            item_pk, item = args
            DynamoCookingStore(COOKING_TABLE).create(item_pk, item)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(create_one, concurrent))
        if store.history_revision(subject) != 11:
            raise AssertionError("Concurrent job creation lost or duplicated a history revision")
        print("prior safety evidence verified; recommendation remains blocked", flush=True)
    finally:
        for pk in keys + [listing_pk]:
            remove_partition(table, pk)
        print("synthetic cleanup complete", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
