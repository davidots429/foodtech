"""Apply standardized synthetic fryer samples to an explicitly bound cooking job."""

from datetime import datetime, timezone
from decimal import Decimal
import os

import boto3
from boto3.dynamodb.types import TypeSerializer

from foodtech_contracts import stream_binding_key
from foodtech_contracts.tonkatsu_process import process_status

from .principal_write_guard import principal_write_guard
from .process_rules import advance
from .process_restatement import DynamoProcessRestatement


class DynamoProcessBridge:
    def __init__(self, table_name: str, region: str = "ap-southeast-2",
                 data_table_name: str | None = None, *, aws_session=None, aws_config=None):
        self.auth_table_name = os.getenv('FOODTECH_AUTH_TABLE', '')
        if not self.auth_table_name:
            raise ValueError('authentication table is required for the process bridge')
        session = aws_session or boto3.Session(region_name=region)
        sdk_options = {"config": aws_config} if aws_config is not None else {}
        self.table = session.resource("dynamodb", **sdk_options).Table(table_name)
        self.client = session.client("dynamodb", **sdk_options)
        self.serializer = TypeSerializer()
        self.restatement = (DynamoProcessRestatement(table_name, data_table_name, region, aws_session=session, aws_config=aws_config)
                            if data_table_name else None)

    def _item(self, value: dict) -> dict:
        return {key: self.serializer.serialize(item) for key, item in value.items()}

    def _owner_guard(self, job_pk: str, job: dict) -> dict:
        prefix, separator, job_id = job_pk.rpartition('#JOB#')
        if not separator or not prefix.startswith('SCOPE#USER#') or not job_id:
            raise ValueError('invalid scoped job key')
        owner = prefix.removeprefix('SCOPE#USER#')
        if not owner or ('owner_subject' in job and job['owner_subject'] != owner) or \
                ('job_id' in job and job['job_id'] != job_id) or not getattr(self, 'auth_table_name', ''):
            raise ValueError('invalid job owner or authentication table')
        return principal_write_guard(self.auth_table_name, owner)

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
            request_status = self._request_restatement(job_pk, assessment)
            if request_status not in {"RESTATEMENT_PENDING", "RESTATEMENT_ALREADY_PENDING"} or \
                    not getattr(self, "restatement", None):
                return request_status
            derived = self.restatement.process(job_pk, event_id)
            if derived in {"DERIVED", "ALREADY_COMPLETED"}:
                return "RESTATEMENT_DERIVED"
            raise RuntimeError(f"late-event derivation must be retried: {derived}")
        event_key = {"pk": job_pk, "sk": f"EVENT#PROCESS#{event_id}"}
        if self.table.get_item(Key=event_key, ConsistentRead=True).get("Item"):
            return self._refresh_restatement_if_needed(job_pk, assessment) or "REPLAYED"
        for _ in range(4):
            old = self.table.get_item(Key={"pk": job_pk, "sk": "META"},
                                      ConsistentRead=True).get("Item")
            if not old or old.get("source_binding") != {
                "source_id": normalized["source_id"], "device_id": normalized["device_id"],
                "batch_id": normalized["batch_id"],
            }:
                return "BINDING_MISMATCH"
            if old["status"] not in {"RUNNING", "PAUSED"}:
                return self._refresh_restatement_if_needed(job_pk, assessment) or "JOB_NOT_ACTIVE"
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
                old.get("process_time_status", "NOT_MEASURED"),
                data_gap=state["data_gap"] or not state["gap_history_verified"],
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
                     "reason_codes": [state["failure_reason"]] if failed and state["failure_reason"] else [],
                     "temperature_status": state["temperature_status"],
                     "rule_version": state["rule_version"], "ingestion_event_id": event_id,
                     "thickness_time_rule_version": old.get("thickness_time_rule_version"),
                     "source_assessment_pk": assessment["pk"],
                     "occurred_at": new["updated_at"]}
            if failed:
                event["validation_change_reason"] = "PROCESS_ANOMALY"
            previous = old.get("process_assessment") or {}
            history_changed = (old.get("process_assessment_status") != overall_status or
                               any(previous.get(field) != state.get(field) for field in
                                   ("temperature_status", "data_gap", "gap_history_verified", "failure_reason")))
            operations = [
                {"Put": {"TableName": self.table.name,
                         "Item": self._item({"pk": job_pk, "sk": "META", **new}),
                         "ConditionExpression": "#version = :expected",
                         "ExpressionAttributeNames": {"#version": "version"},
                         "ExpressionAttributeValues": {":expected": {"N": str(old["version"])}}}},
                {"Put": {"TableName": self.table.name, "Item": self._item(event),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ]
            if history_changed:
                prefix, separator, job_id = job_pk.rpartition("#JOB#")
                if not separator or not prefix.startswith("SCOPE#USER#") or not job_id:
                    raise ValueError("invalid scoped job key")
                history_key = {"pk": f"{prefix}#JOBS", "sk": "HISTORY#REVISION"}
                history = self.table.get_item(Key=history_key, ConsistentRead=True).get("Item")
                revision = history.get("revision") if history else 0
                if isinstance(revision, bool) or not isinstance(revision, (int, Decimal)) or \
                        (isinstance(revision, Decimal) and not revision.is_finite()) or \
                        revision != int(revision) or not (0 if history is None else 1) <= revision < 10**18:
                    raise ValueError("invalid customer history revision")
                put = {"TableName": self.table.name,
                       "Item": self._item({**(history or {}), **history_key, "revision": int(revision) + 1}),
                       "ConditionExpression": "attribute_not_exists(pk)" if history is None else "revision = :prior"}
                if history is not None:
                    put["ExpressionAttributeValues"] = {":prior": {"N": str(int(revision))}}
                operations.append({"Put": put})
            operations.append(self._owner_guard(job_pk, old))
            try:
                self.client.transact_write_items(TransactItems=operations)
                return self._refresh_restatement_if_needed(job_pk, assessment) or overall_status
            except self.client.exceptions.TransactionCanceledException as exc:
                if self.table.get_item(Key=event_key, ConsistentRead=True).get("Item"):
                    return self._refresh_restatement_if_needed(job_pk, assessment) or "REPLAYED"
                reasons = exc.response.get("CancellationReasons", [])
                if len(reasons) == len(operations) and reasons[-1].get('Code') == 'ConditionalCheckFailed':
                    raise RuntimeError('owner is suspended or deletion-locked; retry after reconciliation') from exc
                codes = {reason.get("Code") for reason in reasons}
                retryable = (reasons and codes <= {None, "None", "TransactionConflict", "ConditionalCheckFailed"} and
                             ("TransactionConflict" in codes or reasons[0].get("Code") == "ConditionalCheckFailed" or
                              (history_changed and len(reasons) > 2 and reasons[2].get("Code") == "ConditionalCheckFailed")))
                if not retryable:
                    raise
                # Another writer changed the job version; recalculate from the new state.
        raise RuntimeError("process assessment could not commit after concurrent job updates")

    def _refresh_restatement_if_needed(self, job_pk: str, assessment: dict) -> str | None:
        restatement = getattr(self, "restatement", None)
        if not restatement:
            return None
        event_id = assessment["ingestion_event_id"]
        request_key = {"pk": job_pk, "sk": f"RESTATEMENT#REQUEST#{event_id}"}
        existing = self.table.get_item(Key=request_key, ConsistentRead=True).get("Item")
        if existing and existing["status"] == "COMPLETED":
            return "RESTATEMENT_DERIVED"
        if not existing and not restatement.has_new_source(job_pk, assessment):
            return None
        requested = ("RESTATEMENT_ALREADY_PENDING" if existing else
                     self._request_restatement(job_pk, assessment, reason="FOLLOWUP_SAMPLE"))
        if requested not in {"RESTATEMENT_PENDING", "RESTATEMENT_ALREADY_PENDING"}:
            return requested
        derived = restatement.process(job_pk, event_id)
        if derived in {"DERIVED", "ALREADY_COMPLETED"}:
            return "RESTATEMENT_DERIVED"
        raise RuntimeError(f"follow-up derivation must be retried: {derived}")

    def _request_restatement(self, job_pk: str, assessment: dict,
                             reason: str = "LATE_AFTER_GAP") -> str:
        normalized = assessment["normalized"]
        old = self.table.get_item(Key={"pk": job_pk, "sk": "META"},
                                  ConsistentRead=True).get("Item")
        if not old or old.get("source_binding") != {
                "source_id": normalized["source_id"], "device_id": normalized["device_id"],
                "batch_id": normalized["batch_id"]}:
            return "BINDING_MISMATCH"
        event_id = assessment["ingestion_event_id"]
        key = {"pk": job_pk, "sk": f"RESTATEMENT#REQUEST#{event_id}"}
        if self.table.get_item(Key=key, ConsistentRead=True).get("Item"):
            return "RESTATEMENT_ALREADY_PENDING"
        prefix, separator, job_id = job_pk.rpartition("#JOB#")
        if not separator or not prefix.startswith("SCOPE#USER#") or not job_id:
            raise ValueError("invalid scoped job key")
        requested_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        request = {**key, "status": "PENDING", "reason": reason,
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
            operations = [
                {"Put": {"TableName": self.table.name, "Item": self._item(request),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Update": {"TableName": self.table.name,
                            "Key": self._item({"pk": prefix + "#JOBS", "sk": "HISTORY#REVISION"}),
                            "UpdateExpression": "ADD revision :one",
                            "ConditionExpression": "attribute_not_exists(revision) OR revision < :max",
                            "ExpressionAttributeValues": {":one": {"N": "1"}, ":max": {"N": "1000000000000000000"}}}},
                self._owner_guard(job_pk, old),
            ]
            self.client.transact_write_items(TransactItems=operations)
            return "RESTATEMENT_PENDING"
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get('CancellationReasons', [])
            if len(reasons) == len(operations) and reasons[-1].get('Code') == 'ConditionalCheckFailed':
                raise RuntimeError('owner is suspended or deletion-locked; retry after reconciliation') from exc
            if self.table.get_item(Key=key, ConsistentRead=True).get("Item"):
                return "RESTATEMENT_ALREADY_PENDING"
            raise
