"""Opt-in C# HTTPS Mock -> adapter -> local API -> development DynamoDB test.

The HTTP customer session is synthetic; only random test partitions are removed.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
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


async def scenario(store, partitions):
    subject = "synthetic-combi-mock-" + uuid.uuid4().hex
    ids = [uuid.uuid4() for _ in range(4)]
    pk = f"SCOPE#USER#{subject}#COMBI_MEAL#{ids[0]}"
    partitions.extend((pk, pk + "#LIVE", pk + "#SOURCE"))

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
                server = subprocess.Popen(["dotnet", str(ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"),
                                           "serve", "--fixture", str(source), "--cert", str(pfx), "--port", str(port)],
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
                    mock = RestMockClient(base, "test-client", "test-secret", str(cert), profile_id="mockprofile-120")
                    try:
                        result = await import_meal(mock, api, meal["meal_batch_id"], fixture["deviceId"],
                                                   "validation-csrf", publish_live=True)
                    finally:
                        mock.close()
                finally:
                    server.terminate()
                    server.wait(timeout=5)
            assert result["phase_counts"] == {"RICE": 5, "TRANSITION": 5, "MACKEREL": 5}
            path = f"/api/customer/combi-meals/{meal['meal_batch_id']}"
            live = await api.get(path + "/source-live")
            live.raise_for_status()
            assert live.json()["state"] == "COMPLETED" and live.json()["count"] == 15
            fetched = await api.get(path)
            fetched.raise_for_status()
            assert fetched.json()["meal"]["source"]["raw_sha256"] == result["raw_sha256"]
            assert fetched.json()["mackerel"]["safety_status"] == "NOT_MEASURED"
            return {"phase_counts": result["phase_counts"], "live_count": 15,
                    "final_hash_matches": True, "safety_not_assumed": True}


async def main():
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use AWS_PROFILE=foodtech and the exact development cooking table")
    if not shutil.which("dotnet") or not shutil.which("openssl") or not (
            ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll").is_file():
        raise SystemExit("Build the .NET 10 Mock in Release and put dotnet and openssl on PATH")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != "573693340100":
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
