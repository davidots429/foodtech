"""Validate the fryer read projection without executing or applying a recipe."""
from dataclasses import asdict

from .prior_history_witness import PriorHistoryWitness
from .tonkatsu_execution_binding import BINDING_SCHEMA, parse_tonkatsu_binding
from .tonkatsu_prior_history import TonkatsuPriorHistoryWitness
from .tonkatsu_read_scope import TonkatsuReadScope
from .recipe_execution_scope import validate_recipe_status_payload
from .recipe_result_view import validate_recipe_result_payload


def validate_tonkatsu_result_view(payload, *, execution_id, job_id, result_view):
    if type(payload) is not dict or type(payload.get("source_current")) is not bool:
        raise ValueError("invalid fryer status projection")
    base = {name: value for name, value in payload.items() if name != "source_current"}
    validate = validate_recipe_result_payload if result_view else validate_recipe_status_payload
    checked = validate(base, execution_id=execution_id, job_id=job_id)
    proposal = checked.get("proposal")
    if proposal is not None and (proposal["menu"] != "TONKATSU" or not payload["source_current"]):
        raise ValueError("fryer proposal scope unavailable")
    return {**checked, "source_current": payload["source_current"]}


def fryer_review_scope_payload(scope, *, account, stage):
    return {
        "account": account, "stage": stage, "execution_id": scope.execution_id,
        "execution_version": scope.execution_version,
        "binding": {"schema_version": BINDING_SCHEMA, **asdict(scope.binding)},
        "history": asdict(scope.history),
    }


def parse_fryer_review_scope(payload):
    if type(payload) is not dict or set(payload) != {
        "account", "stage", "execution_id", "execution_version", "binding", "history",
    }:
        raise ValueError("invalid stored fryer review scope")
    witness = dict(payload["history"])
    history = dict(witness["history"])
    history["job_versions"] = tuple(history["job_versions"])
    witness["history"] = PriorHistoryWitness(**history)
    witness["streams"] = tuple(witness["streams"])
    return TonkatsuReadScope(payload["execution_id"], payload["execution_version"],
                            parse_tonkatsu_binding(payload["binding"]), TonkatsuPriorHistoryWitness(**witness))
