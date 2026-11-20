"""Validate only synthetic OPC UA quarantine items in the deployed development stack."""
import hashlib
import json
import os
import uuid

import boto3

from foodtech_collection_adapter.ingestion_quarantine import IngestionQuarantine
from foodtech_collection_adapter.opcua import validate_or_quarantine

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
TABLE = "foodtech-data-flow-dev"
STACK = "FoodtechMinimalDataFlow-dev"


def main():
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    outputs = session.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", [])
    values = {output["OutputKey"]: output["OutputValue"] for output in outputs}
    bucket = values.get("IngestionDlqBucketName")
    if values.get("DataTableName") != TABLE or not bucket:
        raise SystemExit("The development stack does not expose its dedicated ingestion quarantine bucket")
    instance = "mockprofile-203-a-001"
    value = json.dumps(["synthetic-invalid-" + uuid.uuid4().hex])
    payload = value.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"foodtech:opcua:{instance}:{digest}"))
    object_key = f"ingestion/OPCUA/mockprofile-203/{event_id}/{digest}.bin"
    key = {"pk": f"INGESTION_DLQ#{event_id}", "sk": "MANIFEST"}
    table = session.resource("dynamodb").Table(TABLE)
    s3 = session.client("s3")
    if table.get_item(Key=key, ConsistentRead=True).get("Item"):
        raise RuntimeError("Synthetic manifest already exists; no changes made")
    if any(item["Key"] == object_key for item in
           s3.list_objects_v2(Bucket=bucket, Prefix=object_key).get("Contents", [])):
        raise RuntimeError("Synthetic object already exists; no changes made")
    # The random event ID gives this invocation an isolated object and manifest.
    try:
        sink = IngestionQuarantine(bucket, TABLE, REGION)
        for _ in range(2):
            try:
                validate_or_quarantine(value, instance, {}, None, source_id="synthetic-opcua",
                                       profile_id="mockprofile-203", quarantine=sink)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid history was accepted")
        item = table.get_item(Key=key, ConsistentRead=True)["Item"]
        assert item["sha256"] == digest and item["object_key"] == object_key
        assert item["ingestion_failure_reason_code"] == "INVALID_SOURCE_HISTORY"
        response = s3.get_object(Bucket=bucket, Key=object_key)
        try:
            assert response["Body"].read() == payload
            assert response["ServerSideEncryption"] == "AES256"
        finally:
            response["Body"].close()
    finally:
        table.delete_item(Key=key)
        s3.delete_object(Bucket=bucket, Key=object_key)
        if table.get_item(Key=key, ConsistentRead=True).get("Item"):
            raise RuntimeError("Synthetic manifest cleanup failed")
        remaining = s3.list_objects_v2(Bucket=bucket, Prefix=object_key).get("Contents", [])
        if any(item["Key"] == object_key for item in remaining):
            raise RuntimeError("Synthetic object cleanup failed")
    print(json.dumps({"quarantine": "verified", "replay": "verified", "cleanup": "verified", "region": REGION}))


if __name__ == "__main__":
    main()
