"""Resolve protected fryer read scopes from stored execution metadata."""
import re

from foodtech_contracts.collection_evidence import MAX_RECEIPT_RECORDS
from foodtech_contracts.combi_history_witness import canonical_uuid, history_integer
from foodtech_contracts.tonkatsu_execution_binding import parse_tonkatsu_binding
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

    def _source_links(self, owner, binding, streams):
        requests = []
        references = (binding.current, binding.prior)
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
        for index, (stream, reference) in enumerate(zip(streams, references)):
            link, revision = (self._decode(row["Item"]) for row in rows[2 * index:2 * index + 2])
            if (
                link.get("pk") != stream or link.get("sk") != "BINDING"
                or link.get("job_id") != reference.job_id
                or link.get("job_pk") != f"SCOPE#USER#{owner}#JOB#{reference.job_id}"
                or revision.get("pk") != stream or revision.get("sk") != "SOURCE#REVISION"
                or history_integer(revision["revision"], 1, MAX_RECEIPT_RECORDS) != reference.source_revision
            ):
                raise ValueError("fryer source link or version changed")

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
            self._source_links(owner, binding, witness.streams)
            self.history.revalidate_tonkatsu(witness)
            self._source_links(owner, binding, witness.streams)
            if self._execution(execution_id) != execution:
                raise ValueError("fryer execution changed")
            return TonkatsuReadScope(execution_id, version, binding, witness)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, PriorHistoryUnavailable):
            raise TonkatsuScopeUnavailable("verified fryer read scope unavailable") from None
