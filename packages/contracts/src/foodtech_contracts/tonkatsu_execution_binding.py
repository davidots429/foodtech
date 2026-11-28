"""Stored fryer execution references, separate from read authority."""
from dataclasses import dataclass

from .collection_evidence import MAX_RECEIPT_RECORDS
from .combi_history_witness import canonical_uuid, history_integer

BINDING_SCHEMA = "tonkatsu-execution-binding-v1"


@dataclass(frozen=True, repr=False)
class TonkatsuJobReference:
    job_id: str
    job_version: int
    source_revision: int

    def __post_init__(self):
        if not canonical_uuid(self.job_id) or type(self.job_version) is not int or type(self.source_revision) is not int:
            raise ValueError("invalid fryer job reference")
        history_integer(self.job_version)
        history_integer(self.source_revision, 1, MAX_RECEIPT_RECORDS)


@dataclass(frozen=True, repr=False)
class TonkatsuExecutionBinding:
    current: TonkatsuJobReference
    prior: TonkatsuJobReference
    auth_epoch: int

    def __post_init__(self):
        if type(self.current) is not TonkatsuJobReference or type(self.prior) is not TonkatsuJobReference or (
            self.current.job_id == self.prior.job_id or type(self.auth_epoch) is not int
        ):
            raise ValueError("invalid fryer execution binding")
        history_integer(self.auth_epoch, 0, 2**63 - 1)


def parse_tonkatsu_binding(payload):
    if type(payload) is not dict or set(payload) != {"schema_version", "current", "prior", "auth_epoch"}:
        raise ValueError("invalid stored fryer binding fields")
    if payload["schema_version"] != BINDING_SCHEMA:
        raise ValueError("unknown stored fryer binding version")

    def reference(value):
        if type(value) is not dict or set(value) != {"job_id", "job_version", "source_revision"}:
            raise ValueError("invalid stored fryer reference fields")
        return TonkatsuJobReference(value["job_id"], history_integer(value["job_version"]),
                                    history_integer(value["source_revision"], 1, MAX_RECEIPT_RECORDS))

    return TonkatsuExecutionBinding(reference(payload["current"]), reference(payload["prior"]),
                                    history_integer(payload["auth_epoch"], 0, 2**63 - 1))
