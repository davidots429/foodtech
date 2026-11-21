"""Opt-in C# Jeyuk samples -> FIFO Worker -> bound customer job validation."""

import asyncio
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

import boto3
import httpx
from boto3.dynamodb.conditions import Key

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore
from foodtech_business_api.main import app
from foodtech_collection_adapter.jeyuk_aws import DynamoJeyukIngestor
from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker

from jeyuk_dynamodb_check import collect_over_https, csharp_samples
from mock_quality_dynamodb_check import empty_queue
from process_flow_dynamodb_check import (ACCOUNT, COOKING_TABLE, DATA_TABLE, ORDERED_QUEUE,
                                         QUARANTINE_QUEUE, REGION, post, remove_partition,
                                         remove_test_messages)


FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


async def run_check(*, safety: bool = False, size: str = "small", timeline: bool = False,
                    full: bool = False, feed_safety: bool = False, evidence: bool = False,
                    wire: bool = False) -> int:
    if size not in {"small", "medium", "large"} or (not safety and (size != "small" or timeline or full)) or \
            (full and not timeline) or (feed_safety and (not safety or not timeline or size != "small")) or \
            (evidence and not (safety and timeline and full and feed_safety and size == "small")) or \
            (wire and not evidence):
        raise ValueError("medium and large require the safety scenario")
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for the development account.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    sqs = session.client("sqs")
    ordered_url = sqs.get_queue_url(QueueName=ORDERED_QUEUE)["QueueUrl"]
    quarantine_url = sqs.get_queue_url(QueueName=QUARANTINE_QUEUE)["QueueUrl"]
    if not empty_queue(sqs, ordered_url) or not empty_queue(sqs, quarantine_url):
        raise SystemExit("Shared development queues must be empty.")
    run_id = uuid.uuid4().hex[:12]
    fixture_id = f"jeyuk-{'safety' if safety else 'job'}-{run_id}"
    stream_pk = f"JEYUK#{fixture_id}"
    subject = f"synthetic-jeyuk-{run_id}"
    data_table = session.resource("dynamodb").Table(DATA_TABLE)
    cooking_table = session.resource("dynamodb").Table(COOKING_TABLE)
    cooking_store = DynamoCookingStore(COOKING_TABLE, 'foodtech-auth-sessions-dev')
    job_pk = None
    listing_key = None
    profile_id = "jeyuk-small-safety" if evidence else f"live-{run_id}"
    profile_pk = f"SCOPE#USER#{subject}#CONDITION_PROFILE#{profile_id}"
    cursor_key = f"full-wire-{run_id}"

    async def fake_session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)

    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false",
                                     "FOODTECH_COOKING_TABLE": COOKING_TABLE,
                                     "FOODTECH_AUTH_TABLE": "foodtech-auth-sessions-dev",
                                     "FOODTECH_DATA_TABLE": DATA_TABLE}), \
             patch.object(cooking, "_session", side_effect=fake_session), \
             patch.object(cooking, "get_cooking_store", return_value=cooking_store):
            worker = DynamoStandardizationWorker(DATA_TABLE, ordered_url, quarantine_url)
            ingestor = DynamoJeyukIngestor(DATA_TABLE, ordered_url)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://local") as client:
                job = await post(client, "/api/customer/cooking-jobs", {
                    "planned_portions": 2, "recipe_version": "synthetic-jeyuk-live-v1",
                    "equipment_profile": f"jeyuk-{size}",
                    "actual_load_kg": {"small": "1.8", "medium": "12", "large": "60"}[size]}, 201)
                job_pk = cooking._pk(subject, job["job_id"])
                listing_key = {"pk": f"SCOPE#USER#{subject}#JOBS",
                               "sk": f"CREATED#{job['created_at']}#{job['job_id']}"}
                path = f"/api/customer/cooking-jobs/{job['job_id']}"
                job = await post(client, path + "/source-binding", {
                    "expected_version": job["version"], "source_id": "synthetic-rest",
                    "device_id": "mockprofile-110-a-001", "batch_id": fixture_id})
                if safety:
                    job = await post(client, path + "/validation-context", {
                        "schema_version": "jeyuk-validation-context-v1",
                        "expected_version": job["version"],
                        "ingredient_spec_id": "synthetic-pork-v1",
                        "ingredient_lot_id": "synthetic-lot-v1", "supplier_id": "synthetic-supplier",
                        "sensor_id": "synthetic-probe", "sensor_calibration_record_id": "synthetic-cal-v1",
                        "process_condition_profile_id": f"jeyuk-{size}-safety",
                        "process_condition_version": "v1"})
                job = await post(client, path + "/transitions", {
                    "expected_version": job["version"], "event": "PREHEAT_COMPLETED"})
                duration = {"small": 480, "medium": 900, "large": 1500}[size] if full or feed_safety else 61
                placed_at = datetime.now(timezone.utc) - (timedelta(seconds=duration + 10) if timeline else timedelta())
                if feed_safety:
                    for ingredient, mass in (("meat", "1.0"), ("vegetable", "0.5"), ("sauce", "0.3")):
                        job = await post(client, path + f"/jeyuk-feeds/{ingredient}", {
                            "expected_version": job["version"], "started_at": placed_at.isoformat(),
                            "completed_at": placed_at.isoformat(), "mass_kg": mass}, 201)
                job = await post(client, path + "/transitions", {
                    "expected_version": job["version"], "event": "COOKING_STARTED",
                    "actual_placed_at": placed_at.isoformat()})
                history_before = cooking_store.history_revision(subject)
                fixture_kind = "full" if full else "safety"
                fixture_version = "v2" if evidence else "v1"
                fixture_path = FIXTURE_DIR / f"jeyuk-{size}-{fixture_kind}-{fixture_version}.json"
                if wire:
                    count = await asyncio.to_thread(collect_over_https, fixture_id, ordered_url, cursor_key,
                        fixture_path=fixture_path, logical_start_at=placed_at.isoformat(), realtime=False, max_runtime=1800)
                    rows, last_key = [], None
                    while True:
                        args = {"KeyConditionExpression": Key("pk").eq(stream_pk) & Key("sk").begins_with("RAW#"),
                                "ConsistentRead": True}
                        if last_key:
                            args["ExclusiveStartKey"] = last_key
                        page = data_table.query(**args)
                        rows.extend(page.get("Items", []))
                        last_key = page.get("LastEvaluatedKey")
                        if not last_key:
                            break
                    rows.sort(key=lambda row: row["sk"])
                    raws = [row["raw"] for row in rows]
                    if count != 480 or len(raws) != count:
                        raise AssertionError("full Mock collection did not persist all samples")
                else:
                    raws = csharp_samples(fixture_id, fixture_path,
                                          placed_at.isoformat() if timeline else None) if safety else csharp_samples(fixture_id)
                    for index, raw in enumerate(raws, 1):
                        ingestor.ingest(raw)
                        if evidence and (index % 100 == 0 or index == len(raws)):
                            print(f"synthetic ingestion: {index}/{len(raws)}", flush=True)
                processed = []
                references = []
                for _ in range(len(raws) + 16):
                    page = sqs.receive_message(QueueUrl=ordered_url, MaxNumberOfMessages=10,
                                               WaitTimeSeconds=1, VisibilityTimeout=180)
                    for message in page.get("Messages", []):
                        reference = json.loads(message["Body"])
                        if reference.get("pk") != stream_pk:
                            sqs.change_message_visibility(QueueUrl=ordered_url,
                                                          ReceiptHandle=message["ReceiptHandle"],
                                                          VisibilityTimeout=0)
                            raise RuntimeError("unexpected message in shared development ordered queue")
                        try:
                            processed.append(worker.process(reference))
                        except Exception:
                            sqs.change_message_visibility(QueueUrl=ordered_url,
                                                          ReceiptHandle=message["ReceiptHandle"],
                                                          VisibilityTimeout=0)
                            raise
                        references.append(reference)
                        sqs.delete_message(QueueUrl=ordered_url, ReceiptHandle=message["ReceiptHandle"])
                        if evidence and (len(processed) % 100 == 0 or len(processed) == len(raws)):
                            print(f"synthetic processing: {len(processed)}/{len(raws)}", flush=True)
                    if len(processed) == len(raws):
                        break
                if len(processed) != len(raws):
                    raise AssertionError("not all Jeyuk samples reached the bound job")
                if cooking_store.history_revision(subject) <= history_before:
                    raise AssertionError("Worker did not advance customer history for changed process status")
                final = (await client.get(path)).json()["job"]
                if safety:
                    if ((processed[59]["auto_safety_status"], processed[59]["mixture_hold_seconds"]) !=
                            ("PENDING", 59) or
                            (processed[60]["auto_safety_status"], processed[60]["mixture_hold_seconds"]) !=
                            ("PASSED", 60) or
                            (final["status"], final["auto_safety_status"], final["evaluation_blocked"]) !=
                            ("RUNNING", "PASSED", True)):
                        raise AssertionError("post-addition sixty-second safety hold was not recorded")
                    if full or feed_safety:
                        if full and (len(raws) != duration or final["process_assessment"]["second"] != duration):
                            raise AssertionError("the complete synthetic cooking stream was not processed")
                        final = await post(client, path + "/transitions", {
                            "expected_version": final["version"], "event": "COOKING_COMPLETED",
                            "actual_lifted_at": (placed_at + timedelta(seconds=duration)).isoformat()})
                        if final["status"] != "COMPLETED" or final["process_time_status"] != "NORMAL":
                            raise AssertionError("the full synthetic cooking job was not completed normally")
                    if feed_safety:
                        lifted = placed_at + timedelta(seconds=duration)
                        probes = [{"phase": phase, "probe_id": f"probe-{index}",
                                   "meat_sample_id": f"sample-{index}", "thickest_or_clumped_part": True,
                                   "sampled_at": (lifted + timedelta(seconds=index)).isoformat(),
                                   "calibration_status": "OK", "celsius": "75"}
                                  for index, phase in enumerate(("EARLY", "MIDDLE", "LATE"), 1)]
                        final = await post(client, path + "/safety/discharge-check", {
                            "expected_version": final["version"], "kind": "INITIAL", "samples": probes})
                        if final["safety_status"] != "PASSED" or final.get("jeyuk_feed_safety_aligned") is not True:
                            raise AssertionError("bound feed timing did not pass the safety check")
                        if evidence:
                            await post(client, "/api/customer/cooking-jobs/condition-profiles", {
                                "profile_id": profile_id, "profile_version": "v1", "equipment_profile": "jeyuk-small",
                                "heating_setpoint_celsius": "200", "target_rpm": "30"}, 201)
                            if not wire:
                                tracker = JeyukCompletionTracker(0)
                                for raw in raws:
                                    tracker.record(raw)
                                receipt = tracker.finish(cursor=len(raws), complete=True, explicit=True)
                                if not receipt:
                                    raise AssertionError("could not construct synthetic completion receipt")
                                data_table.put_item(Item={"pk": stream_pk, "sk": "COLLECTION#RECEIPT#live-test", **receipt},
                                                    ConditionExpression="attribute_not_exists(pk)")
                            response = await client.get(path + "/validation-evidence")
                            response.raise_for_status()
                            observation = response.json()
                            if observation["status"] != "LINKED_UNVERIFIED" or observation["activation_allowed"]:
                                raise AssertionError(f"full source evidence was not linked: {observation['missing_checks']}")
                            saved = await post(client, path + "/validation-evidence-snapshots", {
                                "expected_version": final["version"]}, 201)
                            listed = await client.get(path + "/validation-evidence-snapshots")
                            listed.raise_for_status()
                            if saved["activation_allowed"] or not any(
                                    item["snapshot_id"] == saved["snapshot_id"] for item in listed.json()):
                                raise AssertionError("unverified evidence snapshot was not listed")
                            print(json.dumps({"scenario": "FULL_EVIDENCE", "processed": len(raws),
                                              "evidence_status": observation["status"],
                                              "activation_allowed": saved["activation_allowed"], "mock_https": wire}), flush=True)
                            return 0
                        final = await post(client, path + "/jeyuk-feeds/sauce", {
                            "expected_version": final["version"], "started_at": placed_at.isoformat(),
                            "completed_at": (placed_at + timedelta(seconds=2)).isoformat(),
                            "mass_kg": "0.3", "reason": "synthetic mismatch"}, 201)
                        if final["safety_status"] != "INCOMPLETE" or not final["evaluation_blocked"]:
                            raise AssertionError("feed correction did not invalidate safety")
                        final = await post(client, path + "/safety/discharge-check", {
                            "expected_version": final["version"], "kind": "REVALIDATION", "samples": probes})
                        if final["safety_status"] != "INCOMPLETE" or final["jeyuk_feed_safety_aligned"]:
                            raise AssertionError("mismatched final feed must not pass safety")
                    evidence_response = await client.get(path + "/validation-evidence")
                    if evidence_response.status_code != 200:
                        raise AssertionError(f"source evidence read failed: {evidence_response.text}")
                    evidence = evidence_response.json()
                    if evidence["checks"]["source_completion_record_matched"] or evidence["source_revision"] is not None:
                        raise AssertionError("missing collection receipt must not authorize a source revision")
                    tracker = JeyukCompletionTracker(0)
                    for raw in raws:
                        tracker.record(raw)
                    receipt = tracker.finish(cursor=len(raws), complete=True, explicit=True)
                    if not receipt:
                        raise AssertionError("could not construct synthetic completion receipt")
                    data_table.put_item(Item={"pk": stream_pk, "sk": "COLLECTION#RECEIPT#live-test", **receipt},
                                        ConditionExpression="attribute_not_exists(pk)")
                    evidence_response = await client.get(path + "/validation-evidence")
                    evidence_response.raise_for_status()
                    evidence = evidence_response.json()
                    if not evidence["checks"]["source_completion_record_matched"] or \
                            not evidence["checks"]["source_revision_stable"] or evidence["source_revision"] != len(raws):
                        raise AssertionError("completed source revision was not confirmed")
                    receipt_ref = evidence["source_receipt_reference"]
                    profile = {"profile_id": f"live-{run_id}", "profile_version": "v1",
                               "equipment_profile": f"jeyuk-{size}", "heating_setpoint_celsius": "200",
                               "target_rpm": "30"}
                    cooking_store.create_condition_profile(subject, {**profile, "provenance": "SYNTHETIC",
                                                                      "status": "DECLARED_UNVERIFIED"})
                    snapshot = {"sk": f"VALIDATION#EVIDENCE#{uuid.uuid4()}", "status": "LINKED_UNVERIFIED",
                                "job_version": final["version"], "source_revision": len(raws),
                                "source_receipt_reference": receipt_ref, "condition_profile_reference": profile}
                    await asyncio.to_thread(cooking_store.save_validation_evidence, job_pk, final["version"], snapshot,
                                            DATA_TABLE, len(raws), receipt_ref)
                    if not cooking_table.get_item(Key={"pk": job_pk, "sk": snapshot["sk"]}, ConsistentRead=True).get("Item"):
                        raise AssertionError("evidence snapshot was not persisted")
                    profile_key = {"pk": profile_pk, "sk": "VERSION#v1"}
                    cooking_table.update_item(Key=profile_key, UpdateExpression="SET target_rpm = :changed",
                                              ExpressionAttributeValues={":changed": "31"})
                    try:
                        stale = {**snapshot, "sk": f"VALIDATION#EVIDENCE#{uuid.uuid4()}"}
                        try:
                            await asyncio.to_thread(cooking_store.save_validation_evidence, job_pk, final["version"], stale,
                                                    DATA_TABLE, len(raws), receipt_ref)
                        except Conflict:
                            pass
                        else:
                            raise AssertionError("changed condition profile was accepted")
                    finally:
                        cooking_table.update_item(Key=profile_key, UpdateExpression="SET target_rpm = :original",
                                                  ExpressionAttributeValues={":original": profile["target_rpm"]})
                    receipt_key = {"pk": stream_pk, "sk": "COLLECTION#RECEIPT#live-test"}
                    data_table.update_item(Key=receipt_key, UpdateExpression="SET ordered_raw_sha256 = :bad",
                                           ExpressionAttributeValues={":bad": "0" * 64})
                    try:
                        stale = {**snapshot, "sk": f"VALIDATION#EVIDENCE#{uuid.uuid4()}"}
                        try:
                            await asyncio.to_thread(cooking_store.save_validation_evidence, job_pk, final["version"], stale,
                                                    DATA_TABLE, len(raws), receipt_ref)
                        except Conflict:
                            pass
                        else:
                            raise AssertionError("changed completion receipt was accepted")
                    finally:
                        data_table.update_item(Key=receipt_key, UpdateExpression="SET ordered_raw_sha256 = :original",
                                               ExpressionAttributeValues={":original": receipt_ref["ordered_raw_sha256"]})
                    for check in ("source_records_contiguous", "source_records_match_binding",
                                  "source_derived_results_match_raw", "source_safety_samples_valid"):
                        if not evidence["checks"][check]:
                            raise AssertionError(f"source evidence check failed: {check}")
                    if timeline:
                        recorded = data_table.get_item(Key={"pk": stream_pk, "sk": "RAW#000061"}, ConsistentRead=True)["Item"]
                        observed = datetime.fromisoformat(json.loads(recorded["raw"])["observedAt"].replace("Z", "+00:00"))
                        if observed != placed_at + timedelta(seconds=61) or \
                                evidence["checks"]["source_process_span_covers_cooking"] != full:
                            raise AssertionError("stored synthetic time or cooking timeline assessment is incorrect")
                    if evidence["activation_allowed"] or evidence["checks"]["job_completed"] != (full or feed_safety):
                        raise AssertionError("the synthetic job must not activate validation")
                elif (processed[4]["job_result"] != "WARNING" or
                      processed[29]["job_result"] != "FAILED" or
                      (final["status"], final["process_assessment_status"],
                       final["process_assessment"]["second"]) != ("FAILED", "FAILED", 30)):
                    raise AssertionError("Jeyuk warning or failure did not reach the bound job")
                history_before_replay = cooking_store.history_revision(subject)
                if worker.process(references[-1])["job_result"] != "REPLAYED":
                    raise AssertionError("replayed assessment changed the bound job")
                if cooking_store.history_revision(subject) != history_before_replay:
                    raise AssertionError("replayed assessment advanced customer history")
                print(json.dumps({"processed": len(processed), "scenario": "SAFETY" if safety else "PROCESS", "size": size, "timeline": timeline, "full": full,
                                  "feed_safety": feed_safety, "job_status": final["status"],
                                  "auto_safety_status": final["auto_safety_status"],
                                  "replay": "REPLAYED"}), flush=True)
                return 0
    finally:
        errors = []
        try:
            remove_test_messages(sqs, ordered_url, stream_pk)
        except Exception as exc:
            errors.append(f"queue cleanup: {exc}")
        try:
            remove_partition(data_table, stream_pk)
            if wire:
                remove_partition(data_table, f"MOCK#mockprofile-110-a-001#{cursor_key}")
            remove_partition(cooking_table, stream_pk)
            remove_partition(cooking_table, profile_pk)
            remove_partition(cooking_table, f"SCOPE#USER#{subject}#CONDITION_PROFILES")
            if job_pk:
                remove_partition(cooking_table, job_pk)
            if listing_key:
                cooking_table.delete_item(Key=listing_key)
                if cooking_table.get_item(Key=listing_key, ConsistentRead=True).get("Item"):
                    raise AssertionError("synthetic job list entry remains")
        except Exception as exc:
            errors.append(f"table cleanup: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        print("synthetic cleanup complete", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--safety", action="store_true")
    parser.add_argument("--size", choices=("small", "medium", "large"), default="small")
    parser.add_argument("--timeline", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--feed-safety", action="store_true")
    parser.add_argument("--evidence", action="store_true")
    parser.add_argument("--wire", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run_check(safety=args.safety, size=args.size, timeline=args.timeline, full=args.full,
                                 feed_safety=args.feed_safety, evidence=args.evidence, wire=args.wire))


if __name__ == "__main__":
    sys.exit(main())
