"""Opt-in C# HTTPS Mock -> adapter -> local API -> development DynamoDB test.

The HTTP customer session is synthetic; only random test partitions are removed.
"""
import asyncio
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import boto3
import httpx

from foodtech_business_api import combi_meal, cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from foodtech_collection_adapter.combi_import import import_meal
from foodtech_collection_adapter.rest import RestMockClient
from combi_live_dynamodb_check import remove_and_verify

ROOT = Path(__file__).resolve().parents[2]
TABLE = "foodtech-cooking-jobs-dev"


async def collector_child(subject, meal_id, device_id, base, cert):
    """Run the collector in a fresh process with a synthetic local API session."""
    store = DynamoCookingStore(TABLE, "foodtech-auth-sessions-dev")
    async def session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)
    with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
         patch.object(cooking, "_session", side_effect=session), \
         patch.object(cooking, "get_cooking_store", return_value=store):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as api:
            mock = RestMockClient(base, "test-client", "test-secret", cert, profile_id="mockprofile-120")
            try:
                result = await import_meal(mock, api, meal_id, device_id, "validation-csrf", publish_live=True)
                print(json.dumps(result), flush=True)
            finally:
                mock.close()


async def restart_collector(api, subject, meal, device_id, base, cert):
    command = [sys.executable, __file__, "--collector-child", subject, meal["meal_batch_id"], device_id, base, cert]
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    partial = None
    try:
        for _ in range(160):
            if first.poll() is not None:
                raise RuntimeError(f"first collector exited early: {first.stderr.read()[-1000:]}")
            response = await api.get(f"/api/customer/combi-meals/{meal['meal_batch_id']}/source-live")
            response.raise_for_status()
            count = response.json().get("count", 0)
            if 0 < count < 15:
                partial = count
                break
            await asyncio.sleep(0.25)
        if partial is None:
            raise TimeoutError("collector did not persist a partial live stream")
        if first.poll() is not None:
            raise RuntimeError("first collector exited before the forced interruption")
    finally:
        if first.poll() is None:
            first.kill()
        await asyncio.to_thread(first.communicate, timeout=10)
    if first.returncode != -9:
        raise AssertionError("first collector was not forcibly terminated")
    second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = await asyncio.to_thread(second.communicate, timeout=90)
    finally:
        if second.poll() is None:
            second.kill()
            await asyncio.to_thread(second.communicate, timeout=10)
    if second.returncode != 0:
        raise RuntimeError(f"restarted collector failed: {stderr[-1000:]}")
    return json.loads(stdout.strip().splitlines()[-1]), partial


async def scenario(store, partitions, *, reassess=False, realtime=False, resume=False, restart=False):
    subject = "synthetic-combi-mock-" + uuid.uuid4().hex
    ids = [uuid.uuid4() for _ in range(4)]
    pk = f"SCOPE#USER#{subject}#COMBI_MEAL#{ids[0]}"
    partitions.extend((pk, pk + "#LIVE", pk + "#SOURCE", f"SCOPE#USER#{subject}#COMBI_MEALS"))

    async def session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)

    with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
         patch.object(cooking, "_session", side_effect=session), \
         patch.object(cooking, "get_cooking_store", return_value=store), \
         patch.object(combi_meal, "uuid4", side_effect=ids):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as api:
            response = await api.post("/api/customer/combi-meals", headers={"X-CSRF-Token": "validation-csrf"}, json={
                "equipment_size": "MEDIUM_FIXED_RACK_10", "rack_count": 1, "pan_count": 10,
                "dry_rice_kg": "20", "drained_rice_kg": "24", "added_water_kg": "20",
                "pan_tare_kg": "10", "rack_tare_kg": "0"})
            response.raise_for_status()
            meal = response.json()["meal"]
            assert meal["meal_batch_id"] == str(ids[0])
            fixture = json.loads((ROOT / "fixtures/combi-normal-medium-v1.json").read_text())
            fixture["mealBatchId"] = meal["meal_batch_id"]
            for phase, key in zip(fixture["phases"],
                                  ("rice_cooking_job_id", "mode_transition_id", "mackerel_cooking_job_id")):
                phase["cookingJobId"] = meal[key]
                if not reassess or phase["phase"] != "RICE":
                    phase["segments"][0]["seconds"] = 5
            with tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                source, key, cert, pfx = (temp / name for name in ("fixture.json", "key.pem", "cert.pem", "cert.pfx"))
                source.write_text(json.dumps(fixture))
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                                "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                                "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key),
                                "-in", str(cert), "-password", "pass:test-password"],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-client",
                       "FOODTECH_MOCK_CLIENT_SECRET": "test-secret", "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
                command = ["dotnet", str(ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"),
                           "serve", "--fixture", str(source), "--cert", str(pfx), "--port", str(port)]
                if realtime:
                    command.append("--realtime")
                server = subprocess.Popen(command,
                                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    base = f"https://localhost:{port}"
                    for _ in range(50):
                        if server.poll() is not None:
                            raise RuntimeError("Mock exited before becoming healthy")
                        try:
                            if httpx.get(base + "/health", verify=str(cert), timeout=0.3).status_code == 200:
                                break
                        except httpx.TransportError:
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("Mock health did not become available")
                    if restart:
                        result, partial = await restart_collector(api, subject, meal, fixture["deviceId"], base, str(cert))
                        if result["phase_counts"] != {"RICE": 5, "TRANSITION": 5, "MACKEREL": 5}:
                            raise AssertionError("unexpected phase counts after process restart")
                        final = await api.get(f"/api/customer/combi-meals/{meal['meal_batch_id']}")
                        final.raise_for_status()
                        live = await api.get(f"/api/customer/combi-meals/{meal['meal_batch_id']}/source-live")
                        live.raise_for_status()
                        if final.json()["meal"]["source"]["raw_sha256"] != result["raw_sha256"] or \
                                live.json().get("count") != 15 or live.json().get("state") != "COMPLETED":
                            raise AssertionError("restarted collector did not finalize the saved source")
                        return {"phase_counts": result["phase_counts"], "persisted_before_restart": partial,
                                "process_restart": True, "live_count": 15, "final_hash_matches": True}
                    mock = RestMockClient(base, "test-client", "test-secret", str(cert), profile_id="mockprofile-120")
                    try:
                        interrupted_count = None
                        if resume:
                            original_post = api.post
                            async def interrupted_post(url, **kwargs):
                                response = await original_post(url, **kwargs)
                                if str(url).endswith("/source-live") and response.is_success:
                                    raise RuntimeError("synthetic collector interruption after durable save")
                                return response
                            with patch.object(api, "post", side_effect=interrupted_post):
                                try:
                                    await import_meal(mock, api, meal["meal_batch_id"], fixture["deviceId"],
                                                      "validation-csrf", publish_live=True)
                                except RuntimeError as error:
                                    if str(error) != "synthetic collector interruption after durable save":
                                        raise
                                else:
                                    raise AssertionError("collector was not interrupted")
                            saved = await api.get(f"/api/customer/combi-meals/{meal['meal_batch_id']}/source-live")
                            saved.raise_for_status()
                            interrupted_count = saved.json().get("count")
                            if not isinstance(interrupted_count, int) or not 0 < interrupted_count < 15:
                                raise AssertionError("durable partial collection was not found")
                            original_poll = mock.poll
                            def changed_prefix(*args, **kwargs):
                                page = original_poll(*args, **kwargs)
                                if page.records and args[0] == 0:
                                    return replace(page, records=(page.records[0] + " ", *page.records[1:]))
                                return page
                            with patch.object(mock, "poll", side_effect=changed_prefix):
                                try:
                                    await import_meal(mock, api, meal["meal_batch_id"], fixture["deviceId"],
                                                      "validation-csrf", publish_live=True)
                                except ValueError as error:
                                    if "differs from the saved live snapshot" not in str(error):
                                        raise
                                else:
                                    raise AssertionError("changed previously saved source was accepted")
                            unchanged = await api.get(f"/api/customer/combi-meals/{meal['meal_batch_id']}/source-live")
                            unchanged.raise_for_status()
                            if unchanged.json().get("count") != interrupted_count:
                                raise AssertionError("rejected source changed the saved cursor")
                        task = asyncio.create_task(import_meal(mock, api, meal["meal_batch_id"], fixture["deviceId"],
                                                               "validation-csrf", publish_live=not reassess))
                        observed_partial = False
                        try:
                            while realtime and not task.done():
                                observation = await api.get(f"/api/customer/combi-meals/{meal['meal_batch_id']}/source-live")
                                observation.raise_for_status()
                                live = observation.json()
                                if 0 < live.get("count", 0) < 15 and live.get("state") != "COMPLETED":
                                    observed_partial = True
                                await asyncio.sleep(0.25)
                            result = await task
                        finally:
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                        if realtime and not observed_partial:
                            raise AssertionError("No in-progress live samples were observed")
                    finally:
                        mock.close()
                finally:
                    server.terminate()
                    server.wait(timeout=5)
            assert result["phase_counts"] == {"RICE": 1800 if reassess else 5, "TRANSITION": 5, "MACKEREL": 5}
            path = f"/api/customer/combi-meals/{meal['meal_batch_id']}"
            if not reassess:
                live = await api.get(path + "/source-live")
                live.raise_for_status()
                assert live.json()["state"] == "COMPLETED" and live.json()["count"] == 15
            fetched = await api.get(path)
            fetched.raise_for_status()
            assert fetched.json()["meal"]["source"]["raw_sha256"] == result["raw_sha256"]
            assert fetched.json()["mackerel"]["safety_status"] == "NOT_MEASURED"
            if reassess:
                headers = {"X-CSRF-Token": "validation-csrf"}
                start = datetime.fromisoformat(fixture["logicalStartAt"].replace("Z", "+00:00"))
                version = fetched.json()["meal"]["version"]
                for event, offset in (("RICE_STARTED", 0), ("RICE_PROGRAM_COMPLETED", 1800), ("RICE_UNLOADED", 1800)):
                    reply = await api.post(path + "/events", headers=headers, json={
                        "expected_version": version, "event": event, "observed_at": (start + timedelta(seconds=offset)).isoformat()})
                    reply.raise_for_status(); version = reply.json()["meal"]["version"]
                assessed = await api.post(path + "/source-assessment/rice", headers=headers, json={"expected_version": version})
                assessed.raise_for_status(); version = assessed.json()["meal"]["version"]
                assert assessed.json()["step"]["monitored_process_assessment"]["fan_observation"]["status"] == "MATCHED"
                checked = await api.post(path + "/rice-safety", headers=headers, json={
                    "expected_version": version, "observation": "NO_ANOMALY", "note": "합성 확인"})
                checked.raise_for_status(); version = checked.json()["meal"]["version"]
                assert checked.json()["step"]["safety_status"] == "PASSED"
                quality = await api.post(path + "/quality/rice", headers=headers, json={
                    "expected_version": version, "expected_current_version": 0, "kind": "INITIAL",
                    "scores": {name: 4 for name in cooking.SCORE_ITEMS},
                    "observations": {name: "합성 관찰" for name in cooking.SCORE_ITEMS},
                    "quality_decision": "QUALITY_PASSED", "planned_portions": 3, "actual_portions": 3})
                quality.raise_for_status(); version = quality.json()["meal"]["version"]
                assert quality.json()["step"]["quality_current"]["usable"]
                # Recreate the pre-fan schema only in this randomly named synthetic item.
                store.table.update_item(Key={"pk": pk, "sk": f"JOB#RICE#{meal['rice_cooking_job_id']}"},
                    UpdateExpression="REMOVE monitored_process_assessment.fan_observation")
                masked = await api.get(path); masked.raise_for_status()
                assert masked.json()["rice"]["safety_status"] == "INCOMPLETE"
                updated = await api.post(path + "/source-assessment/rice", headers=headers, json={
                    "expected_version": version, "reassess_legacy_fan": True})
                updated.raise_for_status(); version = updated.json()["meal"]["version"]
                assert updated.json()["step"]["monitored_process_assessment"]["fan_observation"]["status"] == "MATCHED"
                assert updated.json()["step"]["safety_status"] == "INCOMPLETE"
                assert not updated.json()["step"]["quality_current"]["usable"]
                assert "fan_observation" not in updated.json()["event"]["previous_assessment"]
                checked = await api.post(path + "/rice-safety", headers=headers, json={
                    "expected_version": version, "observation": "NO_ANOMALY", "note": "재확인"})
                checked.raise_for_status()
                assert checked.json()["step"]["safety_status"] == "PASSED"
                assert not checked.json()["step"]["quality_current"]["usable"]
                return {"phase_counts": result["phase_counts"], "legacy_reassessment": "verified",
                        "prior_quality_restored": False, "final_hash_matches": True}
            return {"phase_counts": result["phase_counts"], "live_count": 15,
                    "realtime_partial_observed": observed_partial if realtime else None,
                    "resumed_after_saved_count": interrupted_count,
                    "changed_prefix_rejected": True if resume else None,
                    "final_hash_matches": True, "safety_not_assumed": True}


async def main(*, reassess=False, realtime=False, resume=False, restart=False):
    if (reassess and realtime) or ((resume or restart) and not realtime) or (resume and restart):
        raise SystemExit("Choose realtime or legacy reassessment, not both")
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use AWS_PROFILE=foodtech and the exact development cooking table")
    if not shutil.which("dotnet") or not shutil.which("openssl") or not (
            ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll").is_file():
        raise SystemExit("Build the .NET 10 Mock in Release and put dotnet and openssl on PATH")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != "573693340100":
        raise SystemExit("Unexpected AWS account")
    store = DynamoCookingStore(TABLE, "foodtech-auth-sessions-dev")
    partitions = []
    try:
        result = await scenario(store, partitions, reassess=reassess, realtime=realtime, resume=resume, restart=restart)
    finally:
        remove_and_verify(store.table, partitions)
    print(json.dumps({**result, "cleanup": "verified", "table": TABLE}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reassess", action="store_true")
    parser.add_argument("--realtime", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--collector-child", nargs=5, metavar=("SUBJECT", "MEAL", "DEVICE", "BASE", "CERT"))
    args = parser.parse_args()
    if args.collector_child:
        asyncio.run(collector_child(*args.collector_child))
    else:
        asyncio.run(main(reassess=args.reassess, realtime=args.realtime, resume=args.resume, restart=args.restart))
