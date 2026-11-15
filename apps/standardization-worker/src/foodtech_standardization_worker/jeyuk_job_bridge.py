"""Apply an explicitly bound Jeyuk process decision to a customer cooking job."""

from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer

from foodtech_contracts.jeyuk_process import RULE_VERSION


class DynamoJeyukJobBridge:
    def __init__(self, table_name: str, region: str = "ap-southeast-2"):
        session = boto3.Session(region_name=region)
        self.table = session.resource("dynamodb").Table(table_name)
        self.client = session.client("dynamodb")
        self.serializer = TypeSerializer()

    def apply(self, result: dict) -> str:
        fixture_id = result["fixture_id"]
        second = int(result["second"])
        stream_pk = f"JEYUK#{fixture_id}"
        binding = self.table.get_item(
            Key={"pk": stream_pk, "sk": "BINDING"}, ConsistentRead=True,
        ).get("Item")
        if not binding:
            return "UNBOUND"
        job_pk = binding["job_pk"]
        event_key = {"pk": job_pk, "sk": f"EVENT#JEYUK#{second:06d}"}
        if self.table.get_item(Key=event_key, ConsistentRead=True).get("Item"):
            return "REPLAYED"
        for _ in range(4):
            old = self.table.get_item(Key={"pk": job_pk, "sk": "META"},
                                      ConsistentRead=True).get("Item")
            if (not old or old.get("menu") != "JEYUK" or old.get("source_binding") != {
                    "source_id": "synthetic-rest", "device_id": "mockprofile-110-a-001",
                    "batch_id": fixture_id} or old.get("jeyuk_size_profile") != result["profile"]
                    or old.get("actual_load_kg") is None
                    or Decimal(str(old.get("actual_load_kg"))) != Decimal(result["actual_load_kg"])):
                return "BINDING_MISMATCH"
            if old["status"] not in {"RUNNING", "PAUSED"}:
                return "JOB_NOT_ACTIVE"
            previous = old.get("process_assessment", {}).get("second", 0)
            if second <= previous:
                return "OLD_SAMPLE"
            if second != previous + 1:
                raise ValueError("Jeyuk job process samples must be consecutive")
            process_status = result["process_status"]
            failed = process_status == "FAILED"
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            new = {**old, "version": int(old["version"]) + 1, "updated_at": now,
                   "process_assessment_status": process_status,
                   "process_surface_status": result["surface_band"],
                   "process_rpm_status": result["rpm_band"],
                   "process_load_status": result["load_status"],
                   "process_assessment": {"second": second, "status": process_status,
                                          "reason_codes": result["reason_codes"],
                                          "rule_version": RULE_VERSION,
                                          "source_assessment_pk": stream_pk,
                                          "source_assessment_sk": f"ASSESSMENT#{second:06d}"}}
            if failed:
                new["status"] = "FAILED"
                new["job_state_version"] = int(old["job_state_version"]) + 1
                new["evaluation_blocked"] = True
                new["last_transition_event"] = "PROCESS_FAILURE_DETECTED"
                new["last_transition_reason"] = ",".join(result["reason_codes"])
            event = {**event_key, "domain": "PROCESS_ASSESSMENT",
                     "trigger": "PROCESS_FAILURE_DETECTED" if failed else "PROCESS_SAMPLE_ASSESSED",
                     "from_status": old.get("process_assessment_status", "INSUFFICIENT_DATA"),
                     "to_status": process_status, "reason_codes": result["reason_codes"],
                     "source_assessment_pk": stream_pk,
                     "source_assessment_sk": f"ASSESSMENT#{second:06d}",
                     "rule_version": RULE_VERSION, "occurred_at": now}
            try:
                self.client.transact_write_items(TransactItems=[
                    {"Put": {"TableName": self.table.name,
                             "Item": self._item({"pk": job_pk, "sk": "META", **new}),
                             "ConditionExpression": "#version = :expected",
                             "ExpressionAttributeNames": {"#version": "version"},
                             "ExpressionAttributeValues": {":expected": {"N": str(old["version"])}}}},
                    {"Put": {"TableName": self.table.name, "Item": self._item(event),
                             "ConditionExpression": "attribute_not_exists(pk)"}},
                ])
                return process_status
            except self.client.exceptions.TransactionCanceledException as exc:
                if self.table.get_item(Key=event_key, ConsistentRead=True).get("Item"):
                    return "REPLAYED"
                reasons = exc.response.get("CancellationReasons", [])
                if not reasons or reasons[0].get("Code") != "ConditionalCheckFailed":
                    raise
        raise RuntimeError("Jeyuk process decision could not commit after concurrent job updates")

    def _item(self, value: dict) -> dict:
        return {key: self.serializer.serialize(item) for key, item in value.items()}
