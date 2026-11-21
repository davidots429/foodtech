"""Persist synthetic Jeyuk raw samples and hand off references through SQS FIFO."""

import hashlib
import json
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample


class DynamoJeyukIngestor:
    def __init__(self, table_name: str, queue_url: str, region: str = "ap-southeast-2"):
        session = boto3.Session(region_name=region)
        self.table = session.resource("dynamodb").Table(table_name)
        self.sqs = session.client("sqs")
        self.queue_url = queue_url

    @staticmethod
    def _cursor_pk(cursor_key: str) -> str:
        return f"MOCK#mockprofile-110-a-001#{cursor_key}"

    def cursor(self, cursor_key: str) -> int:
        item = self.table.get_item(
            Key={"pk": self._cursor_pk(cursor_key), "sk": "CURSOR"}, ConsistentRead=True,
        ).get("Item")
        return int(item["cursor"]) if item else 0

    def save_cursor(self, cursor: int, cursor_key: str) -> None:
        self.table.update_item(
            Key={"pk": self._cursor_pk(cursor_key), "sk": "CURSOR"},
            UpdateExpression="SET #cursor = :cursor",
            ExpressionAttributeNames={"#cursor": "cursor"},
            ExpressionAttributeValues={":cursor": cursor},
        )

    def save_completion_receipt(self, receipt: dict, cursor_key: str) -> None:
        self.table.put_item(
            Item={"pk": f"JEYUK#{receipt['fixture_id']}", "sk": f"COLLECTION#RECEIPT#{uuid.uuid4()}",
                  **receipt, "cursor_key": cursor_key, "recorded_at": datetime.now(timezone.utc).isoformat()},
            ConditionExpression=Attr("pk").not_exists(),
        )

    def ingest(self, raw: str) -> dict:
        sample = decode_jeyuk_mock_sample(raw)
        pk = f"JEYUK#{sample.fixture_id}"
        raw_sk = f"RAW#{sample.second:06d}"
        key = {"pk": pk, "sk": raw_sk}
        item = {**key, "raw": raw, "profile_id": "mockprofile-110",
                "fixture_id": sample.fixture_id, "second": sample.second,
                "provenance": "SYNTHETIC"}
        try:
            self.table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            existing = self.table.get_item(Key=key, ConsistentRead=True)["Item"]
            if existing["raw"] != raw:
                raise ValueError("Jeyuk raw sample changed for the same fixture and second")
        reference = {"profile_id": "mockprofile-110", "pk": pk, "raw_sk": raw_sk,
                     "fixture_id": sample.fixture_id, "second": sample.second}
        body = json.dumps(reference, separators=(",", ":"))
        dedup_id = hashlib.sha256(f"{pk}|{sample.second}".encode()).hexdigest()
        self.sqs.send_message(
            QueueUrl=self.queue_url, MessageBody=body,
            MessageGroupId=sample.fixture_id, MessageDeduplicationId=dedup_id,
        )
        return reference
