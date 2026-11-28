"""Validate the fryer read projection without executing or applying a recipe."""
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
