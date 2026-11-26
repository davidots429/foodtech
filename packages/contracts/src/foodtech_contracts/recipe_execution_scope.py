"""Allowlisted internal recipe binding observation, never an access grant."""
from dataclasses import dataclass, field
import re
from uuid import UUID


@dataclass(frozen=True, repr=False)
class RecipeExecutionBinding:
    execution_id: str
    tenant_id: str
    feature: str
    job_id: str
    job_version: int
    prior_job_id: str
    prior_job_version: int
    jeyuk_source_revision: int
    prior_source_revision: int


@dataclass(frozen=True)
class RecipeExecutionObservation:
    execution: RecipeExecutionBinding = field(repr=False)
    state: str
    version: int


BINDING_FIELDS = ('execution_id', 'tenant_id', 'feature', 'job_id', 'job_version',
                  'prior_job_id', 'prior_job_version', 'jeyuk_source_revision', 'prior_source_revision')


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def parse_recipe_execution_observation(payload, *, execution_id, tenant_id):
    """Reject mismatched, terminal, incomplete and unexpected server metadata."""
    if not _uuid(execution_id) or not isinstance(tenant_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', tenant_id) or \
            not isinstance(payload, dict) or set(payload) != {*BINDING_FIELDS, 'state', 'version'} or \
            payload.get('execution_id') != execution_id or payload.get('tenant_id') != tenant_id or \
            payload.get('feature') != 'RECIPE_CORRECTION' or not isinstance(payload.get('state'), str) or \
            payload['state'] not in {'PENDING', 'RESERVED'} or \
            type(payload.get('version')) is not int or not 1 <= payload['version'] < 2**63 or \
            any(not _uuid(payload.get(key)) for key in ('job_id', 'prior_job_id')) or \
            payload['job_id'] == payload['prior_job_id'] or any(
                type(payload.get(key)) is not int or not 1 <= payload[key] <= 10**18 for key in
                ('job_version', 'prior_job_version', 'jeyuk_source_revision', 'prior_source_revision')):
        raise ValueError('recipe execution scope unavailable')
    return RecipeExecutionObservation(RecipeExecutionBinding(**{key: payload[key] for key in BINDING_FIELDS}),
                                      payload['state'], payload['version'])


def recipe_execution_observation_payload(snapshot, *, execution_id, tenant_id):
    execution = getattr(snapshot, 'execution', None)
    payload = {key: getattr(execution, key, None) for key in BINDING_FIELDS}
    payload.update(state=getattr(snapshot, 'state', None), version=getattr(snapshot, 'version', None))
    parse_recipe_execution_observation(payload, execution_id=execution_id, tenant_id=tenant_id)
    return payload
