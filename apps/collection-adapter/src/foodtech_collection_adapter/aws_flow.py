"""DynamoDB-backed short resequencer and SQS FIFO handoff for one Mock profile."""

import json
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer

from foodtech_contracts import decode_record, stream_key

from .resequence import Received, Resequencer, StreamState


class DynamoOrderedIngestor:
    def __init__(self, table_name: str, queue_url: str, region: str = "ap-southeast-2"):
        session = boto3.Session(region_name=region)
        self.table = session.resource("dynamodb").Table(table_name)
        self.ddb = session.client("dynamodb")
        self.sqs = session.client("sqs")
        self.queue_url = queue_url
        self.serializer = TypeSerializer()

    def cursor(self) -> int:
        item = self.table.get_item(
            Key={"pk": "MOCK#mockprofile-100-a-001", "sk": "CURSOR"}, ConsistentRead=True,
        ).get("Item")
        return int(item["cursor"]) if item else 0

    def save_cursor(self, cursor: int) -> None:
        self.table.update_item(
            Key={"pk": "MOCK#mockprofile-100-a-001", "sk": "CURSOR"},
            UpdateExpression="SET #cursor = :cursor",
            ExpressionAttributeNames={"#cursor": "cursor"},
            ExpressionAttributeValues={":cursor": cursor},
        )

    def ingest(self, raw: str, now: float | None = None) -> list[Received]:
        record = decode_record(raw)
        now = time.time() if now is None else now
        event_id = str(uuid.uuid4())
        key = stream_key(record)
        received = Received(
            event_id, raw, record["sourceId"], record["deviceId"], record["batchId"],
            record["sequenceNumber"], now,
        )
        raw_item = {
            "pk": key, "sk": f"RAW#{event_id}", "ingestion_event_id": event_id,
            "raw": raw, "received_at_ms": int(now * 1000), "sequence_number": received.sequence_number,
            "source_id": received.source_id, "device_id": received.device_id,
            "batch_id": received.batch_id, "provenance": "SYNTHETIC",
        }
        hold_item = {
            "pk": key, "sk": f"HOLD#{received.sequence_number:012d}#{event_id}",
            "ingestion_event_id": event_id, "received_at_ms": int(now * 1000),
            "sequence_number": received.sequence_number,
        }
        self.ddb.transact_write_items(TransactItems=[
            {"Put": {"TableName": self.table.name, "Item": self._marshal(raw_item),
                     "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Put": {"TableName": self.table.name, "Item": self._marshal(hold_item),
                     "ConditionExpression": "attribute_not_exists(pk)"}},
        ])
        return self.drain(key, now)

    def drain(self, stream: str, now: float | None = None) -> list[Received]:
        now = time.time() if now is None else now
        now_ms = int(now * 1000)
        owner = str(uuid.uuid4())
        try:
            state_item = self.table.update_item(
                Key={"pk": stream, "sk": "STATE"},
                UpdateExpression="SET lease_owner = :owner, lease_until_ms = :until, "
                                 "expected_sequence = if_not_exists(expected_sequence, :one)",
                ConditionExpression=Attr("lease_until_ms").not_exists() | Attr("lease_until_ms").lt(now_ms),
                ExpressionAttributeValues={":owner": owner, ":until": now_ms + 30_000, ":one": 1},
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return []
        state = StreamState(
            expected=int(state_item["expected_sequence"]),
            gap_deadline=float(state_item["gap_deadline_ms"]) / 1000
            if state_item.get("gap_deadline_ms") is not None else None,
            closed_gaps=[tuple(map(int, span)) for span in state_item.get("closed_gaps", [])],
        )
        engine = Resequencer()
        holds = []
        page = self.table.query(KeyConditionExpression=Key("pk").eq(stream) & Key("sk").begins_with("HOLD#"),
                                ConsistentRead=True)
        holds.extend(page.get("Items", []))
        while "LastEvaluatedKey" in page:
            page = self.table.query(KeyConditionExpression=Key("pk").eq(stream) & Key("sk").begins_with("HOLD#"),
                                    ConsistentRead=True, ExclusiveStartKey=page["LastEvaluatedKey"])
            holds.extend(page.get("Items", []))
        released: list[Received] = []
        for item in holds:
            raw_item = self.table.get_item(
                Key={"pk": stream, "sk": f"RAW#{item['ingestion_event_id']}"}, ConsistentRead=True,
            )["Item"]
            event = Received(
                item["ingestion_event_id"], raw_item["raw"], raw_item["source_id"],
                raw_item["device_id"], raw_item["batch_id"], int(item["sequence_number"]),
                int(item["received_at_ms"]) / 1000,
            )
            engine.streams.setdefault((event.source_id, event.device_id, event.batch_id), state)
            released.extend(engine.accept(event, now))
        released.extend(engine.tick(now))
        for event in released:
            body = json.dumps({
                "pk": stream, "raw_sk": f"RAW#{event.ingestion_event_id}",
                "ingestion_event_id": event.ingestion_event_id,
                "sequence_status": event.sequence_status, "gap_before": event.gap_before,
            }, separators=(",", ":"))
            self.sqs.send_message(
                QueueUrl=self.queue_url, MessageBody=body,
                MessageGroupId=event.device_id, MessageDeduplicationId=event.ingestion_event_id,
            )
        self.table.update_item(
            Key={"pk": stream, "sk": "STATE"},
            UpdateExpression="SET expected_sequence = :expected, gap_deadline_ms = :deadline, "
                             "closed_gaps = :gaps REMOVE lease_owner, lease_until_ms",
            ConditionExpression=Attr("lease_owner").eq(owner),
            ExpressionAttributeValues={
                ":expected": state.expected,
                ":deadline": int(state.gap_deadline * 1000) if state.gap_deadline is not None else None,
                ":gaps": [list(span) for span in state.closed_gaps],
            },
        )
        for event in released:
            self.table.delete_item(Key={"pk": stream, "sk": f"HOLD#{event.sequence_number:012d}#{event.ingestion_event_id}"})
        return released

    def _marshal(self, item: dict) -> dict:
        return {key: self.serializer.serialize(value) for key, value in item.items()}
