"""SQS FIFO worker with immutable raw, assessment, and finding records."""

import hashlib
import json
import os
import sys
from dataclasses import asdict

import boto3
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer

from .process import Assessment, Finding, QUALITY_RULE_VERSION, assess
from .process_bridge import DynamoProcessBridge


class DynamoStandardizationWorker:
    def __init__(self, table_name: str, queue_url: str, dlq_url: str, region: str = "ap-southeast-2"):
        session = boto3.Session(region_name=region)
        self.table = session.resource("dynamodb").Table(table_name)
        self.ddb = session.client("dynamodb")
        self.sqs = session.client("sqs")
        self.queue_url = queue_url
        self.dlq_url = dlq_url
        self.serializer = TypeSerializer()
        cooking_table = os.getenv("FOODTECH_COOKING_TABLE")
        self.process_bridge = DynamoProcessBridge(cooking_table, region) if cooking_table else None

    def run_once(self) -> list[dict]:
        page = self.sqs.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
            VisibilityTimeout=30,
        )
        results = []
        for message in page.get("Messages", []):
            result = self.process(json.loads(message["Body"]))
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
            results.append(result)
        return results

    def process(self, reference: dict) -> dict:
        event_id = reference["ingestion_event_id"]
        event_pk = f"EVENT#{event_id}"
        assessment_key = {"pk": event_pk, "sk": "ASSESSMENT#v1"}
        existing = self.table.get_item(Key=assessment_key, ConsistentRead=True).get("Item")
        if existing:
            if existing["quality_status"] == "QUARANTINED":
                self._send_dlq(reference, existing)
            process_result = self._publish_process(existing)
            return {"ingestion_event_id": event_id, "quality_status": existing["quality_status"],
                    "dedup_status": existing["dedup_status"], "replayed": True,
                    "process_result": process_result}
        raw_item = self.table.get_item(
            Key={"pk": reference["pk"], "sk": reference["raw_sk"]}, ConsistentRead=True,
        )["Item"]
        evaluation = assess(
            event_id, raw_item["raw"], sequence_status=reference["sequence_status"],
            gap_before=tuple(reference.get("gap_before", [])),
        )
        dedup_status = self._deduplicate(evaluation)
        findings = list(evaluation.findings)
        if dedup_status == "CONFLICT":
            findings.append(Finding(
                "DUPLICATE_KEY_CONFLICT", "FT-Q-DEDUP-CONFLICT", QUALITY_RULE_VERSION,
                "same business key has different normalized content",
            ))
        quality = "QUARANTINED" if evaluation.quality_status == "QUARANTINED" else \
            "WARNING" if findings else "VALID"
        normalized = evaluation.normalized if dedup_status == "UNIQUE" and quality != "QUARANTINED" else None
        assessment_item = {
            "pk": event_pk, "sk": "ASSESSMENT#v1", "ingestion_event_id": event_id,
            "source_pk": reference["pk"], "raw_sk": reference["raw_sk"],
            "business_dedup_key": evaluation.business_dedup_key,
            "payload_hash": evaluation.payload_hash, "dedup_status": dedup_status,
            "quality_status": quality, "is_quarantined": quality == "QUARANTINED",
            "dlq_reference_id": event_id if quality == "QUARANTINED" else None,
            "raw_values": evaluation.raw_values, "corrected_values": evaluation.corrected_values,
            "correction_rule_id": evaluation.correction_rule_id,
            "mapping_version": evaluation.mapping_version, "provenance": evaluation.provenance,
            "normalized": normalized, "finding_codes": [item.code for item in findings],
        }
        writes = [{"Put": {"TableName": self.table.name, "Item": self._marshal(assessment_item),
                           "ConditionExpression": "attribute_not_exists(pk)"}}]
        for index, finding in enumerate(findings, 1):
            finding_item = {"pk": event_pk, "sk": f"FINDING#{index:03d}",
                            "assessment_sk": "ASSESSMENT#v1", **asdict(finding)}
            writes.append({"Put": {"TableName": self.table.name, "Item": self._marshal(finding_item),
                                   "ConditionExpression": "attribute_not_exists(pk)"}})
        try:
            self.ddb.transact_write_items(TransactItems=writes)
        except self.ddb.exceptions.TransactionCanceledException:
            existing = self.table.get_item(Key=assessment_key, ConsistentRead=True).get("Item")
            if not existing:
                raise
            assessment_item = existing
        if assessment_item["quality_status"] == "QUARANTINED":
            self._send_dlq(reference, assessment_item)
        process_result = self._publish_process(assessment_item)
        return {"ingestion_event_id": event_id, "quality_status": assessment_item["quality_status"],
                "dedup_status": assessment_item["dedup_status"], "replayed": False,
                "process_result": process_result}

    def _publish_process(self, assessment: dict) -> str:
        bridge = getattr(self, "process_bridge", None)
        return bridge.apply(assessment) if bridge else "NOT_CONFIGURED"

    def _deduplicate(self, evaluation: Assessment) -> str:
        key_hash = hashlib.sha256(evaluation.business_dedup_key.encode("utf-8")).hexdigest()
        key = {"pk": f"DEDUP#{key_hash}", "sk": "CANONICAL"}
        try:
            self.table.put_item(
                Item={**key, "business_dedup_key": evaluation.business_dedup_key,
                      "payload_hash": evaluation.payload_hash,
                      "first_ingestion_event_id": evaluation.ingestion_event_id},
                ConditionExpression=Attr("pk").not_exists(),
            )
            return "UNIQUE"
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            existing = self.table.get_item(Key=key, ConsistentRead=True)["Item"]
            if existing["business_dedup_key"] != evaluation.business_dedup_key:
                raise ValueError("business key hash collision")
            if existing["first_ingestion_event_id"] == evaluation.ingestion_event_id:
                return "UNIQUE"
            return "DUPLICATE" if existing["payload_hash"] == evaluation.payload_hash else "CONFLICT"

    def _send_dlq(self, reference: dict, assessment: dict) -> None:
        self.sqs.send_message(
            QueueUrl=self.dlq_url,
            MessageBody=json.dumps({"ingestion_event_id": reference["ingestion_event_id"],
                                    "assessment_pk": assessment["pk"], "assessment_sk": assessment["sk"],
                                    "reason": "QUALITY_QUARANTINED"}, separators=(",", ":")),
            MessageGroupId=reference["pk"],
            MessageDeduplicationId=reference["ingestion_event_id"],
        )

    def _marshal(self, item: dict) -> dict:
        return {key: self.serializer.serialize(value) for key, value in item.items()}


def main() -> int:
    required = ("FOODTECH_DATA_TABLE", "FOODTECH_ORDERED_QUEUE_URL", "FOODTECH_STANDARDIZATION_DLQ_URL")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit("missing configuration: " + ", ".join(missing))
    worker = DynamoStandardizationWorker(
        os.environ["FOODTECH_DATA_TABLE"], os.environ["FOODTECH_ORDERED_QUEUE_URL"],
        os.environ["FOODTECH_STANDARDIZATION_DLQ_URL"],
    )
    print(json.dumps(worker.run_once(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
