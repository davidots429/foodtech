"""Apply standardized synthetic fryer samples to an explicitly bound cooking job."""

from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer
from foodtech_contracts import stream_binding_key
from foodtech_contracts.tonkatsu_process import process_status

from .process_rules import advance


class DynamoProcessBridge:
    def __init__(self, table_name: str, region: str = "ap-southeast-2"):
        session = boto3.Session(region_name=region)
        self.table = session.resource("dynamodb").Table(table_name)
        self.client = session.client("dynamodb")
        self.serializer = TypeSerializer()

    def _item(self, value: dict) -> dict:
        return {key: self.serializer.serialize(item) for key, item in value.items()}

    def apply(self, assessment: dict) -> str:
        normalized = assessment.get("normalized")
        if assessment.get("dedup_status") != "UNIQUE" or not normalized or \
                assessment.get("quality_status") == "QUARANTINED":
            return "SKIPPED_QUALITY"
        if normalized.get("device_status") != "RUNNING":
            return "DEVICE_NOT_RUNNING"
        stream_pk = stream_binding_key(normalized["source_id"], normalized["device_id"],
                                       normalized["batch_id"])
        binding = self.table.get_item(Key={"pk": stream_pk, "sk": "BINDING"},
                                      ConsistentRead=True).get("Item")
        if not binding:
            return "UNBOUND"
        job_pk = binding["job_pk"]
        event_id = assessment["ingestion_event_id"]
        if "LATE_AFTER_GAP" in assessment.get("finding_codes", []):
            return self._request_restatement(job_pk, assessment)
        event_key = {"pk": job_pk, "sk": f"EVENT#PROCESS#{event_id}"}
        if self.table.get_item(Key=event_key, ConsistentRead=True).get("Item"):
            return "REPLAYED"
        for _ in range(4):
            old = self.table.get_item(Key={"pk": job_pk, "sk": "META"},
                                      ConsistentRead=True).get("Item")
            if not old or old.get("source_binding") != {
                "source_id": normalized["source_id"], "device_id": normalized["device_id"],
                "batch_id": normalized["batch_id"],
            }:
                return "BINDING_MISMATCH"
            if old["status"] not in {"RUNNING", "PAUSED"}:
                return "JOB_NOT_ACTIVE"
            state = advance(old.get("process_assessment"), normalized,
                            ingestion_event_id=event_id,
                            quality_status=assessment["quality_status"],
                            finding_codes=set(assessment.get("finding_codes", [])))
            if state is None:
                return "OLD_OR_INVALID_SAMPLE"
            if old.get("process_assessment", {}).get("last_ingestion_event_id") == event_id:
                return "REPLAYED"
            overall_status = process_status(
                state["temperature_status"], old.get("process_thickness_status", "NOT_MEASURED"),
                old.get("process_time_status", "NOT_MEASURED"), data_gap=state["data_gap"],
            )
            failed = overall_status == "FAILED"
            new = {**old, "version": int(old["version"]) + 1,
                   "process_assessment": state, "process_assessment_status": overall_status,
                   "process_temperature_status": state["temperature_status"],
                   "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            if failed:
                new["status"] = "FAILED"
                new["job_state_version"] = int(old["job_state_version"]) + 1
                new["evaluation_blocked"] = True
                new["last_transition_event"] = "PROCESS_FAILURE_DETECTED"
                new["last_transition_reason"] = state["failure_reason"]
            event = {**event_key, "domain": "PROCESS_ASSESSMENT",
                     "trigger": "PROCESS_FAILURE_DETECTED" if failed else "PROCESS_SAMPLE_ASSESSED",
                     "from_status": old.get("process_assessment_status", "INSUFFICIENT_DATA"),
                     "to_status": overall_status,
                     "reason": state["failure_reason"] if failed else None,
                     "temperature_status": state["temperature_status"],
                     "rule_version": state["rule_version"], "ingestion_event_id": event_id,
                     "thickness_time_rule_version": old.get("thickness_time_rule_version"),
                     "source_assessment_pk": assessment["pk"],
                     "occurred_at": new["updated_at"]}
            operations = [
                {"Put": {"TableName": self.table.name,
                         "Item": self._item({"pk": job_pk, "sk": "META", **new}),
                         "ConditionExpression": "#version = :expected",
                         "ExpressionAttributeNames": {"#version": "version"},
                         "ExpressionAttributeValues": {":expected": {"N": str(old["version"])}}}},
                {"Put": {"TableName": self.table.name, "Item": self._item(event),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ]
            try:
                self.client.transact_write_items(TransactItems=operations)
                return overall_status
            except self.client.exceptions.TransactionCanceledException as exc:
                if self.table.get_item(Key=event_key, ConsistentRead=True).get("Item"):
                    return "REPLAYED"
                reasons = exc.response.get("CancellationReasons", [])
                if not reasons or reasons[0].get("Code") != "ConditionalCheckFailed":
                    raise
                # Another writer changed the job version; recalculate from the new state.
        raise RuntimeError("process assessment could not commit after concurrent job updates")

    def _request_restatement(self, job_pk: str, assessment: dict) -> str:
        normalized = assessment["normalized"]
        old = self.table.get_item(Key={"pk": job_pk, "sk": "META"},
                                  ConsistentRead=True).get("Item")
        if not old or old.get("source_binding") != {
                "source_id": normalized["source_id"], "device_id": normalized["device_id"],
                "batch_id": normalized["batch_id"]}:
            return "BINDING_MISMATCH"
        event_id = assessment["ingestion_event_id"]
        key = {"pk": job_pk, "sk": f"RESTATEMENT#REQUEST#{event_id}"}
        requested_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        request = {**key, "status": "PENDING", "reason": "LATE_AFTER_GAP",
                   "ingestion_event_id": event_id,
                   "source_assessment_pk": assessment["pk"],
                   "source_assessment_sk": assessment.get("sk", "ASSESSMENT#v1"),
                   "source_stream_pk": assessment.get("source_pk"),
                   "job_id": old["job_id"], "job_version_at_detection": int(old["version"]),
                   "process_status_at_detection": old.get("process_assessment_status"),
                   "sequence_number": int(normalized["sequence_number"]),
                   "event_time": normalized["event_time"], "requested_at": requested_at,
                   "provenance": "SYNTHETIC"}
        try:
            self.table.put_item(Item=request, ConditionExpression=Attr("pk").not_exists())
            return "RESTATEMENT_PENDING"
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return "RESTATEMENT_ALREADY_PENDING"
