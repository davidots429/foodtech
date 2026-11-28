"""Atomic predicates for a verified fryer execution and its live source scope."""
import math
import re
from copy import deepcopy
from dataclasses import asdict

from foodtech_contracts.prior_history_witness import HISTORY_CONTRACT
from foodtech_contracts.tonkatsu_execution_binding import BINDING_SCHEMA
from foodtech_contracts.tonkatsu_read_scope import TonkatsuReadScope

REGION = "ap-southeast-2"


class _Condition:
    def __init__(self):
        from boto3.dynamodb.types import TypeSerializer

        self.names = {}
        self.values = {}
        self.terms = []
        self.serializer = TypeSerializer()

    def _path(self, parts):
        names = []
        for part in parts.split("."):
            alias = f"#fryer{len(self.names)}"
            self.names[alias] = part
            names.append(alias)
        return ".".join(names)

    def compare(self, path, value, operator="="):
        token = f":fryer{len(self.values)}"
        self.values[token] = self.serializer.serialize(value)
        self.terms.append(f"{self._path(path)} {operator} {token}")

    def missing(self, path):
        self.terms.append(f"attribute_not_exists({self._path(path)})")

    def missing_or_false(self, path):
        attribute = self._path(path)
        token = f":fryer{len(self.values)}"
        self.values[token] = self.serializer.serialize(False)
        self.terms.append(f"(attribute_not_exists({attribute}) OR {attribute} = {token})")

    def missing_or_null(self, path):
        attribute = self._path(path)
        token = f":fryer{len(self.values)}"
        self.values[token] = self.serializer.serialize(None)
        self.terms.append(f"(attribute_not_exists({attribute}) OR {attribute} = {token})")

    def fields(self):
        return {"ConditionExpression": " AND ".join(self.terms), "ExpressionAttributeNames": self.names,
                "ExpressionAttributeValues": self.values}

    def check(self, table, pk, sk=None):
        key = {"pk": {"S": pk}}
        if sk is not None:
            key["sk"] = {"S": sk}
        return {"ConditionCheck": {"TableName": table, "Key": key, **self.fields(),
                                   "ReturnValuesOnConditionCheckFailure": "NONE"}}


def _equals(values):
    condition = _Condition()
    for path, value in values.items():
        condition.compare(path, value)
    return condition


class TonkatsuTransactionConditions:
    def __init__(self, *, scope, account, stage, clock):
        if (type(scope) is not TonkatsuReadScope or type(account) is not str or type(stage) is not str
            or not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z0-9-]{1,32}", stage)
            or not callable(clock)
        ):
            raise ValueError("verified fryer transaction scope required")
        self.scope = scope
        self.execution_table = f"foodtech-ai-execution-registry-{stage}"
        self.auth = f"foodtech-auth-sessions-{stage}"
        self.cooking = f"foodtech-cooking-jobs-{stage}"
        self.data = f"foodtech-data-flow-{stage}"
        self.cooking_arn = f"arn:aws:dynamodb:{REGION}:{account}:table/{self.cooking}"
        self.clock = clock

    def execution_predicate(self, *, completed_calls):
        """Merge into the update on EXEC META to avoid two actions on the same row."""
        if type(completed_calls) is not int or not 0 <= completed_calls <= 3:
            raise ValueError("invalid fryer reservation progression")
        scope = self.scope
        version = scope.execution_version + completed_calls
        if version >= 2**63:
            raise ValueError("fryer execution version overflow")
        current, prior = scope.binding.current, scope.binding.prior
        condition = _equals({
            "version": version, "state": "RESERVED" if completed_calls else "PENDING",
            "tenant_id": scope.history.history.owner, "execution_id": scope.execution_id,
            "feature": "RECIPE_CORRECTION", "data_classification": "SYNTHETIC",
            "job_id": current.job_id, "job_version": current.job_version,
            "prior_job_id": prior.job_id, "prior_job_version": prior.job_version,
            "tonkatsu_binding": {"schema_version": BINDING_SCHEMA, **asdict(scope.binding)},
            "estimated_cost_usd": 0, "external_transfer_approved": False,
        })
        for field in ("combi_binding", "jeyuk_source_revision", "prior_source_revision", "comparison_id",
                      "credential_id", "external_transfer_consent_id"):
            condition.missing_or_null(field)
        if completed_calls:
            condition.compare("reserved_provider", "ollama")
        if completed_calls > 1:
            condition.compare("retry_count", completed_calls - 1)
        else:
            condition.missing("retry_count")
        return condition.fields()

    def bind_execution_update(self, operation, *, completed_calls):
        """Attach the fryer predicate to the registry's existing execution update."""
        if type(operation) is not dict or set(operation) != {"Update"}:
            raise ValueError("fryer execution update required")
        update = deepcopy(operation["Update"])
        if update.get("TableName") != self.execution_table or update.get("Key") != {
            "pk": {"S": "EXEC#" + self.scope.execution_id}, "sk": {"S": "META"},
        }:
            raise ValueError("fryer execution update key mismatch")
        existing = update.get("ConditionExpression")
        if type(existing) is not str or not existing:
            raise ValueError("existing execution update condition required")
        predicate = self.execution_predicate(completed_calls=completed_calls)
        for field in ("ExpressionAttributeNames", "ExpressionAttributeValues"):
            fields = update.setdefault(field, {})
            if type(fields) is not dict or fields.keys() & predicate[field].keys():
                raise ValueError("fryer execution update placeholder conflict")
            fields.update(predicate[field])
        update["ConditionExpression"] = f"({existing}) AND ({predicate['ConditionExpression']})"
        update["ReturnValuesOnConditionCheckFailure"] = "NONE"
        return {"Update": update}

    def review_checks(self):
        scope = self.scope
        result = _equals({"state": "SUCCEEDED", "provider": "ollama", "actual_cost_usd": 0,
                          "settlement_status": "SETTLED"})
        result.compare("expires", int(self.clock()), ">")
        return [*self.source_checks(), _equals({
            "version": scope.execution_version, "state": "SUCCEEDED",
            "tenant_id": scope.history.history.owner, "execution_id": scope.execution_id,
            "tonkatsu_binding": {"schema_version": BINDING_SCHEMA, **asdict(scope.binding)},
            "reserved_provider": "ollama", "actual_cost_usd": 0, "settlement_status": "SETTLED",
        }).check(self.execution_table, "EXEC#" + scope.execution_id, "META"),
            result.check(self.execution_table, "EXEC#" + scope.execution_id, "RESULT")]

    def source_checks(self):
        """Require the same account, writer, roster, jobs, links and revisions."""
        sample = self.clock()
        if type(sample) not in (int, float) or not 1 <= sample < 2**63 or not math.isfinite(sample):
            raise ValueError("invalid fryer predicate clock")
        now = int(sample)
        scope = self.scope
        witness = scope.history
        history = witness.history
        if not witness.writer_verified_at <= now < witness.writer_valid_until:
            raise ValueError("fryer writer attestation expired")

        account = _equals({
            "epoch": scope.binding.auth_epoch, "history_coverage.state": "ACTIVE",
            "history_coverage.version": history.coverage_version,
            "history_coverage.generation": history.generation,
            "history_coverage.writer_contract": HISTORY_CONTRACT,
            "history_coverage.coverage_scope": "ALL_RETAINED_JOBS",
            "history_coverage.writer_manifest_sha256": history.writer_manifest_sha256,
            "history_coverage.coverage_revision": history.coverage_revision,
            "history_coverage.baseline_roster_sha256": history.baseline_roster_sha256,
            "history_coverage.proof_sha256": history.coverage_proof_sha256,
        })
        account.missing_or_false("suspended")
        account.missing("deletion_lock_request")
        writer = _equals({
            "state": "ENFORCED", "version": history.writer_version, "generation": history.generation,
            "writer_contract": HISTORY_CONTRACT, "cooking_table_arn": self.cooking_arn,
            "manifest_sha256": history.writer_manifest_sha256, "verified_at": witness.writer_verified_at,
            "valid_until": witness.writer_valid_until,
        })
        writer.compare("verified_at", now, "<=")
        writer.compare("valid_until", now, ">")
        checks = [
            account.check(self.auth, "USER#" + history.owner),
            writer.check(self.auth, "CONTROL#COOKING_HISTORY_WRITERS"),
            _equals({"revision": history.history_revision}).check(self.cooking,
                f"SCOPE#USER#{history.owner}#JOBS", "HISTORY#REVISION"),
        ]
        for pk, stream, reference in zip(scope.job_partitions, witness.streams,
                                         (scope.binding.current, scope.binding.prior), strict=True):
            job = _equals({"version": reference.job_version, "owner_subject": history.owner,
                "job_id": reference.job_id, "menu": "TONKATSU", "provenance": "SYNTHETIC",
                "equipment_profile": "synthetic-fryer-approved-v2", "source_binding": {
                    "source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
                    "batch_id": stream.rsplit(":", 1)[-1],
                }})
            checks.extend([
                job.check(self.cooking, pk, "META"),
                _equals({"job_pk": pk, "job_id": reference.job_id}).check(self.cooking, stream, "BINDING"),
                _equals({"revision": reference.source_revision}).check(self.data, stream, "SOURCE#REVISION"),
            ])
        return checks
