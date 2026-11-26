"""Apply an explicitly bound Jeyuk process decision to a customer cooking job."""

from datetime import datetime, timezone
from decimal import Decimal
import os

import boto3
from boto3.dynamodb.types import TypeSerializer

from .principal_write_guard import principal_write_guard

from foodtech_contracts.jeyuk_process import RULE_VERSION
from foodtech_contracts.validation_changes import recorded_safety_change


class DynamoJeyukJobBridge:
    def __init__(self, table_name: str, region: str = "ap-southeast-2", *, aws_session=None, aws_config=None):
        self.auth_table_name = os.getenv('FOODTECH_AUTH_TABLE', '')
        if not self.auth_table_name:
            raise ValueError('authentication table is required for the Jeyuk job bridge')
        session = aws_session or boto3.Session(region_name=region)
        sdk_options = {"config": aws_config} if aws_config is not None else {}
        self.table = session.resource("dynamodb", **sdk_options).Table(table_name)
        self.client = session.client("dynamodb", **sdk_options)
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
        if self._recorded_replay(result, stream_pk, event_key):
            return "REPLAYED"
        if result.get("process_rule_version") != RULE_VERSION:
            raise ValueError("Jeyuk assessment rule version requires explicit restatement")
        for _ in range(4):
            old = self.table.get_item(Key={"pk": job_pk, "sk": "META"},
                                      ConsistentRead=True).get("Item")
            if (not old or old.get("menu") != "JEYUK" or old.get("source_binding") != {
                    "source_id": "synthetic-rest", "device_id": "mockprofile-110-a-001",
                    "batch_id": fixture_id} or old.get("jeyuk_size_profile") != result["profile"]
                    or old.get("actual_load_kg") is None
                    or Decimal(str(old.get("actual_load_kg"))) != Decimal(result["actual_load_kg"])):
                return "BINDING_MISMATCH"
            owner = old.get("owner_subject")
            if not isinstance(owner, str) or not owner or job_pk != f"SCOPE#USER#{owner}#JOB#{old.get('job_id')}":
                return "BINDING_MISMATCH"
            if old["status"] not in {"RUNNING", "PAUSED"}:
                return "JOB_NOT_ACTIVE"
            previous_process = old.get("process_assessment", {})
            if previous_process and previous_process.get("rule_version") != RULE_VERSION:
                raise ValueError("Jeyuk job rule version requires explicit restatement")
            previous = previous_process.get("second", 0)
            if second <= previous:
                return "OLD_SAMPLE"
            if second != previous + 1:
                raise ValueError("Jeyuk job process samples must be consecutive")
            process_status = result["process_status"]
            failed = process_status == "FAILED"
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            new = {**old, "version": int(old["version"]) + 1, "updated_at": now,
                   "process_assessment_status": process_status,
                   "auto_safety_status": result.get("auto_safety_status", "NOT_MEASURED"),
                   "mixture_hold_seconds": result.get("mixture_hold_seconds", 0),
                   "safety_passed_at_second": result.get("safety_passed_at_second"),
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
                     "auto_safety_status": result.get("auto_safety_status", "NOT_MEASURED"),
                     "mixture_hold_seconds": result.get("mixture_hold_seconds", 0),
                     "source_assessment_pk": stream_pk,
                     "source_assessment_sk": f"ASSESSMENT#{second:06d}",
                     "rule_version": RULE_VERSION, "occurred_at": now}
            validation_reason = recorded_safety_change(event)
            if validation_reason:
                event["validation_change_reason"] = validation_reason
            previous_reasons = old.get("process_assessment", {}).get("reason_codes", [])
            history_changed = (old.get("process_assessment_status") != process_status or
                               previous_reasons != result["reason_codes"] or
                               old.get("auto_safety_status", "NOT_MEASURED") != result.get("auto_safety_status", "NOT_MEASURED"))
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
                history_key = {"pk": f"SCOPE#USER#{owner}#JOBS", "sk": "HISTORY#REVISION"}
                history = self.table.get_item(Key=history_key, ConsistentRead=True).get("Item")
                revision = history.get("revision") if history else 0
                if isinstance(revision, bool) or not isinstance(revision, (int, Decimal)) or \
                        (isinstance(revision, Decimal) and not revision.is_finite()) or \
                        revision != int(revision) or not (0 if history is None else 1) <= revision < 10**18:
                    raise ValueError("invalid customer history revision")
                put = {"TableName": self.table.name, "Item": self._item({**(history or {}), **history_key, "revision": int(revision) + 1}),
                       "ConditionExpression": "attribute_not_exists(pk)" if history is None else "revision = :prior"}
                if history is not None:
                    put["ExpressionAttributeValues"] = {":prior": {"N": str(int(revision))}}
                operations.append({"Put": put})
            if not getattr(self, 'auth_table_name', ''):
                raise ValueError('authentication table is required for the Jeyuk job bridge')
            operations.append(principal_write_guard(self.auth_table_name, owner))
            try:
                self.client.transact_write_items(TransactItems=operations)
                return process_status
            except self.client.exceptions.TransactionCanceledException as exc:
                if self._recorded_replay(result, stream_pk, event_key):
                    return "REPLAYED"
                reasons = exc.response.get("CancellationReasons", [])
                if len(reasons) == len(operations) and reasons[-1].get("Code") == "ConditionalCheckFailed":
                    raise RuntimeError('owner is suspended or deletion-locked; retry after reconciliation') from exc
                codes = {reason.get("Code") for reason in reasons}
                retryable = (reasons and codes <= {None, "None", "TransactionConflict", "ConditionalCheckFailed"} and
                             ("TransactionConflict" in codes or reasons[0].get("Code") == "ConditionalCheckFailed" or
                              (history_changed and len(reasons) > 2 and reasons[2].get("Code") == "ConditionalCheckFailed")))
                if not retryable:
                    raise
        raise RuntimeError("Jeyuk process decision could not commit after concurrent job updates")

    def _recorded_replay(self, result: dict, stream_pk: str, event_key: dict) -> bool:
        existing = self.table.get_item(Key=event_key, ConsistentRead=True).get("Item")
        if not existing:
            return False
        version = result.get("process_rule_version")
        if not isinstance(version, str) or version not in {"jeyuk-process-v1", RULE_VERSION} or any((
                existing.get("rule_version") != version,
                existing.get("source_assessment_pk") != stream_pk,
                existing.get("source_assessment_sk") != f"ASSESSMENT#{int(result['second']):06d}",
                existing.get("to_status") != result["process_status"],
                existing.get("reason_codes") != result["reason_codes"])):
            raise ValueError("Jeyuk replay does not match its recorded assessment version")
        return True

    def _item(self, value: dict) -> dict:
        return {key: self.serializer.serialize(item) for key, item in value.items()}
