"""Opt-in check of malformed REST bytes, durable quarantine and cursor ordering."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import boto3
import httpx
from botocore.exceptions import ClientError
from foodtech_collection_adapter import __main__ as collector
from process_flow_dynamodb_check import ACCOUNT, DATA_TABLE, ORDERED_QUEUE, REGION, remove_partition

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"
STACK = "FoodtechMinimalDataFlow-dev"
PAYLOAD = b"\xff\xfe\x80"


def main():
    if os.getenv("AWS_PROFILE") != "foodtech" or not shutil.which("dotnet") or not DLL.is_file():
        raise SystemExit("AWS_PROFILE=foodtech and built .NET 10 Mock are required")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    outputs = session.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", [])
    values = {item["OutputKey"]: item["OutputValue"] for item in outputs}
    bucket = values.get("IngestionDlqBucketName")
    if values.get("DataTableName") != DATA_TABLE or not bucket:
        raise SystemExit("The development stack has no dedicated ingestion quarantine bucket")
    ordered = session.client("sqs").get_queue_url(QueueName=ORDERED_QUEUE)["QueueUrl"]
    table, s3 = session.resource("dynamodb").Table(DATA_TABLE), session.client("s3")
    if s3.get_bucket_versioning(Bucket=bucket).get("Status") is not None:
        raise SystemExit("This cleanup procedure requires an unversioned quarantine bucket")
    unique = uuid.uuid4().hex
    cursor = f"live-{unique}"
    cursor_pk = f"MOCK#mockprofile-100-a-001#{cursor}"
    cursor_item = {"pk": cursor_pk, "sk": "CURSOR"}
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"foodtech:rest100:{cursor}:0:{digest}"))
    manifest = {"pk": f"INGESTION_DLQ#{event_id}", "sk": "MANIFEST"}
    object_key = f"ingestion/REST/mockprofile-100/{event_id}/{digest}.bin"
    if table.get_item(Key=manifest, ConsistentRead=True).get("Item") or \
            table.get_item(Key=cursor_item, ConsistentRead=True).get("Item") or \
            s3.list_objects_v2(Bucket=bucket, Prefix=object_key).get("Contents"):
        raise RuntimeError("Synthetic test identifiers already exist")
    try:
        with tempfile.TemporaryDirectory(prefix="foodtech-rest-quarantine-") as directory:
            temp = Path(directory)
            fixture = json.loads((ROOT / "fixtures/mockprofile-100-a-001.json").read_text())
            fixture["records"] = fixture["records"][:1]
            fixture_path, key, cert, pfx = (temp / name for name in ("fixture.json", "key.pem", "cert.pem", "cert.pfx"))
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            password, secret = uuid.uuid4().hex, uuid.uuid4().hex
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key), "-in", str(cert),
                            "-password", f"pass:{password}"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            base = f"https://localhost:{port}"
            server = subprocess.Popen(["dotnet", str(DLL), "serve", "--fixture", str(fixture_path),
                                       "--cert", str(pfx), "--port", str(port), "--fault", "invalid-utf8"],
                                      env={**os.environ, "FOODTECH_MOCK_CLIENT_ID": "synthetic-test",
                                           "FOODTECH_MOCK_CLIENT_SECRET": secret, "FOODTECH_MOCK_CERT_PASSWORD": password},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError("Mock exited before startup")
                    try:
                        if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200:
                            break
                    except httpx.TransportError:
                        time.sleep(.1)
                else:
                    raise TimeoutError("Mock did not start")
                config = {"FOODTECH_MOCK_BASE_URL": base, "FOODTECH_MOCK_CA_FILE": str(cert),
                          "FOODTECH_MOCK_CLIENT_ID": "synthetic-test", "FOODTECH_MOCK_CLIENT_SECRET": secret,
                          "FOODTECH_DATA_TABLE": DATA_TABLE, "FOODTECH_ORDERED_QUEUE_URL": ordered}
                args = ["collector", "--cursor-key", cursor, "--polls", "1"]
                with patch.dict(os.environ, {**config, "FOODTECH_INGESTION_DLQ_BUCKET": f"foodtech-missing-{unique}"}), \
                        patch.object(sys, "argv", args):
                    try:
                        collector.main()
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] != "NoSuchBucket":
                            raise
                    else:
                        raise AssertionError("Missing bucket unexpectedly accepted quarantine")
                if table.get_item(Key=cursor_item, ConsistentRead=True).get("Item") or \
                        table.get_item(Key=manifest, ConsistentRead=True).get("Item"):
                    raise AssertionError("Cursor or manifest advanced after failed quarantine")
                with patch.dict(os.environ, {**config, "FOODTECH_INGESTION_DLQ_BUCKET": bucket}), \
                        patch.object(sys, "argv", args):
                    if collector.main() != 0:
                        raise AssertionError("Collection failed")
                item = table.get_item(Key=manifest, ConsistentRead=True)["Item"]
                if item["sha256"] != digest or item["object_key"] != object_key or \
                        item["ingestion_failure_reason_code"] != "UNSUPPORTED_ENCODING":
                    raise AssertionError("Unexpected quarantine manifest")
                if int(table.get_item(Key=cursor_item, ConsistentRead=True)["Item"]["cursor"]) != 1:
                    raise AssertionError("Cursor did not advance after durable quarantine")
                response = s3.get_object(Bucket=bucket, Key=object_key)
                try:
                    if response["Body"].read() != PAYLOAD or response.get("ServerSideEncryption") != "AES256":
                        raise AssertionError("Original bytes or encryption differ")
                finally:
                    response["Body"].close()
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        table.delete_item(Key=manifest)
        s3.delete_object(Bucket=bucket, Key=object_key)
        remove_partition(table, cursor_pk)
        if table.get_item(Key=manifest, ConsistentRead=True).get("Item") or \
                table.get_item(Key=cursor_item, ConsistentRead=True).get("Item") or \
                s3.list_objects_v2(Bucket=bucket, Prefix=object_key).get("Contents"):
            raise RuntimeError("Synthetic quarantine cleanup failed")
    print(json.dumps({"failed_storage_cursor": "verified", "quarantine": "verified", "cleanup": "verified"}))


if __name__ == "__main__":
    main()
