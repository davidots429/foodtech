"""Resolve protected fryer read scopes from stored execution metadata."""
import re

from foodtech_contracts.collection_evidence import MAX_RECEIPT_RECORDS
from foodtech_contracts.combi_history_witness import canonical_uuid, history_integer
from foodtech_contracts.tonkatsu_execution_binding import (
    TonkatsuExecutionBinding, TonkatsuJobReference, parse_tonkatsu_binding,
)
from foodtech_contracts.tonkatsu_read_scope import TonkatsuReadScope

from .dynamo_prior_history import DynamoPriorHistoryAuthority, PriorHistoryUnavailable
from .subject_identity import valid_subject

_EXECUTION_FIELDS = (
    "pk", "sk", "execution_id", "tenant_id", "feature", "data_classification", "state", "version",
    "job_id", "job_version", "prior_job_id", "prior_job_version", "comparison_id",
    "jeyuk_source_revision", "prior_source_revision", "combi_binding", "tonkatsu_binding",
)


class TonkatsuScopeUnavailable(ValueError):
    pass


class DynamoTonkatsuReadScope:
    def __init__(self, *, execution_table, data_table, history):
        if type(history) is not DynamoPriorHistoryAuthority:
            raise ValueError("protected cooking history reader required")
        stage = history.cooking.removeprefix("foodtech-cooking-jobs-")
        if not re.fullmatch(r"[a-z0-9-]{1,32}", stage) or (
            execution_table, data_table
        ) != (f"foodtech-ai-execution-registry-{stage}", f"foodtech-data-flow-{stage}"):
            raise ValueError("same-stage fryer tables required")
        self.execution_table = execution_table
        self.data_table = data_table
        self.history = history
        self.client = history.client
        self.serializer = history.serializer
        self.decoder = history.decoder

    def _key(self, pk, sk):
        return {name: self.serializer.serialize(value) for name, value in {"pk": pk, "sk": sk}.items()}

    def _decode(self, item):
        return {name: self.decoder.deserialize(value) for name, value in item.items()}

    def _execution(self, execution_id):
        names = {f"#field{index}": name for index, name in enumerate(_EXECUTION_FIELDS)}
        response = self.client.get_item(
            TableName=self.execution_table, Key=self._key("EXEC#" + execution_id, "META"),
            ConsistentRead=True, ProjectionExpression=", ".join(names), ExpressionAttributeNames=names,
        )
        row = self._decode(response["Item"])
        binding = parse_tonkatsu_binding(row["tonkatsu_binding"])
        owner = row.get("tenant_id")
        if (
            row.get("pk") != "EXEC#" + execution_id or row.get("sk") != "META"
            or row.get("execution_id") != execution_id or not valid_subject(owner)
            or row.get("feature") != "RECIPE_CORRECTION" or row.get("data_classification") != "SYNTHETIC"
            or row.get("state") != "PENDING"
            or any(row.get(k) is not None for k in ("comparison_id", "jeyuk_source_revision",
                                                   "prior_source_revision", "combi_binding"))
            or (row.get("job_id"), history_integer(row["job_version"])) !=
                (binding.current.job_id, binding.current.job_version)
            or (row.get("prior_job_id"), history_integer(row["prior_job_version"])) !=
                (binding.prior.job_id, binding.prior.job_version)
        ):
            raise ValueError("stored fryer execution mismatch")
        return owner, binding, history_integer(row["version"], 1, 2**63 - 1)

    def _source_revisions(self, owner, job_ids, streams):
        requests = []
        for stream in streams:
            requests.extend([
                {"Get": {"TableName": self.history.cooking, "Key": self._key(stream, "BINDING"),
                         "ProjectionExpression": "pk, sk, job_pk, job_id"}},
                {"Get": {"TableName": self.data_table, "Key": self._key(stream, "SOURCE#REVISION"),
                         "ProjectionExpression": "pk, sk, #revision",
                         "ExpressionAttributeNames": {"#revision": "revision"}}},
            ])
        rows = self.client.transact_get_items(TransactItems=requests)["Responses"]
        if len(rows) != 4:
            raise ValueError("incomplete fryer source links")
        versions = []
        for index, (stream, job_id) in enumerate(zip(streams, job_ids, strict=True)):
            link, revision = (self._decode(row["Item"]) for row in rows[2 * index:2 * index + 2])
            if (
                link.get("pk") != stream or link.get("sk") != "BINDING"
                or link.get("job_id") != job_id
                or link.get("job_pk") != f"SCOPE#USER#{owner}#JOB#{job_id}"
                or revision.get("pk") != stream or revision.get("sk") != "SOURCE#REVISION"
            ):
                raise ValueError("fryer source link changed")
            versions.append(history_integer(revision["revision"], 1, MAX_RECEIPT_RECORDS))
        return tuple(versions)

    def require_active_account(self, owner, epoch):
        if not valid_subject(owner) or type(epoch) is not int or not 0 <= epoch < 2**63:
            raise TonkatsuScopeUnavailable("authenticated fryer account unavailable")
        response = self.client.get_item(TableName=self.history.auth, Key={"pk": {"S": "USER#" + owner}},
            ProjectionExpression="pk, #epoch, suspended, deletion_lock_request",
            ExpressionAttributeNames={"#epoch": "epoch"}, ConsistentRead=True)
        try:
            row = self._decode(response["Item"])
            if (
                row.get("pk") != "USER#" + owner or history_integer(row.get("epoch"), 0, 2**63 - 1) != epoch
                or row.get("suspended", False) is not False or "deletion_lock_request" in row
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, ArithmeticError):
            raise TonkatsuScopeUnavailable("authenticated fryer account unavailable") from None

    def plan_execution(self, *, owner, epoch, execution_id, current_job_id, prior_job_id, job_versions):
        """Discover source revisions before insertion. The writer must fence this scope atomically."""
        try:
            if not canonical_uuid(execution_id):
                raise ValueError("stored fryer execution identifier required")
            witness = self.history.observe_tonkatsu(owner=owner, auth_epoch=epoch,
                current_job_id=current_job_id, prior_job_id=prior_job_id, job_versions=job_versions)
            current_revision, prior_revision = self._source_revisions(
                owner, (current_job_id, prior_job_id), witness.streams,
            )
            self.history.revalidate_tonkatsu(witness)
            binding = TonkatsuExecutionBinding(
                TonkatsuJobReference(current_job_id, job_versions[0], current_revision),
                TonkatsuJobReference(prior_job_id, job_versions[1], prior_revision), epoch,
            )
            return TonkatsuReadScope(execution_id, 1, binding, witness)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, PriorHistoryUnavailable):
            raise TonkatsuScopeUnavailable("verified fryer execution inputs unavailable") from None

    def resolve(self, *, owner, epoch, execution_id):
        """Bind the broker's authenticated subject and epoch to the stored execution."""
        if not valid_subject(owner) or type(epoch) is not int or not 0 <= epoch < 2**63:
            raise TonkatsuScopeUnavailable("verified fryer read scope unavailable")
        return self._resolve(execution_id, expected_identity=(owner, epoch))

    def resolve_execution(self, execution_id):
        """Resolve Gateway identity from its stored execution after transport authentication."""
        return self._resolve(execution_id)

    def _resolve(self, execution_id, *, expected_identity=None):
        try:
            if not canonical_uuid(execution_id):
                raise ValueError("stored fryer execution required")
            execution = self._execution(execution_id)
            owner, binding, version = execution
            epoch = binding.auth_epoch
            if expected_identity is not None and expected_identity != (owner, epoch):
                raise ValueError("authenticated identity differs from the execution")
            witness = self.history.observe_tonkatsu(owner=owner, auth_epoch=epoch,
                current_job_id=binding.current.job_id, prior_job_id=binding.prior.job_id,
                job_versions=(binding.current.job_version, binding.prior.job_version))
            job_ids = (binding.current.job_id, binding.prior.job_id)
            revisions = (binding.current.source_revision, binding.prior.source_revision)
            if self._source_revisions(owner, job_ids, witness.streams) != revisions:
                raise ValueError("fryer source version changed")
            self.history.revalidate_tonkatsu(witness)
            if self._source_revisions(owner, job_ids, witness.streams) != revisions:
                raise ValueError("fryer source version changed")
            if self._execution(execution_id) != execution:
                raise ValueError("fryer execution changed")
            return TonkatsuReadScope(execution_id, version, binding, witness)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, PriorHistoryUnavailable):
            raise TonkatsuScopeUnavailable("verified fryer read scope unavailable") from None
