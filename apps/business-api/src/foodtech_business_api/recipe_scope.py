"""Resolve a recipe execution's candidate read scope inside a trusted boundary.

This does not issue credentials, authenticate the caller, or authorize a model
call. Callers must supply a freshly authenticated customer session and trusted,
strongly consistent execution and job readers. A later broker must enforce the
returned bounds in AWS, independently of the gateway and request body.
"""
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID


class RecipeScopeDenied(Exception):
    pass


@dataclass(frozen=True)
class RecipeReadScope:
    owner: str = field(repr=False)
    execution_id: str = field(repr=False)
    job_partitions: tuple[str, str] = field(repr=False)
    source_partitions: tuple[str, str] = field(repr=False)


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def resolve_recipe_scope(session, execution_id, execution_reader, job_reader):
    """Observe two stable snapshots; no permission is granted by this function."""
    subject = getattr(session, 'subject', None)
    expires = getattr(session, 'expires', None)
    valid_expiry = (type(expires) is int and 0 < expires < 2**63) or (
        isinstance(expires, Decimal) and expires.is_finite() and 0 < expires < 2**63 and expires == int(expires))
    if getattr(session, 'surface', None) != 'customer' or not isinstance(subject, str) or \
            not re.fullmatch(r'[A-Za-z0-9-]{1,128}', subject) or \
            not valid_expiry or expires <= time.time() or \
            not _uuid(execution_id):
        raise RecipeScopeDenied('recipe scope unavailable')

    def snapshot():
        execution = execution_reader(execution_id)
        if not execution or getattr(execution, 'execution_id', None) != execution_id or \
                getattr(execution, 'tenant_id', None) != subject or \
                getattr(execution, 'feature', None) != 'RECIPE_CORRECTION':
            raise RecipeScopeDenied('recipe scope unavailable')
        ids = (getattr(execution, 'job_id', None), getattr(execution, 'prior_job_id', None))
        versions = (getattr(execution, 'job_version', None), getattr(execution, 'prior_job_version', None))
        revisions = (getattr(execution, 'jeyuk_source_revision', None), getattr(execution, 'prior_source_revision', None))
        if not all(_uuid(value) for value in ids) or ids[0] == ids[1] or \
                any(type(value) is not int or not 1 <= value <= 10**18 for value in (*versions, *revisions)):
            raise RecipeScopeDenied('recipe scope unavailable')
        jobs, sources = [], []
        for job_id, version in zip(ids, versions):
            pk = f'SCOPE#USER#{subject}#JOB#{job_id}'
            job = job_reader(pk)
            if not isinstance(job, dict) or job.get('owner_subject') != subject or job.get('job_id') != job_id or \
                    type(job.get('version')) is not int or job['version'] != version or \
                    job.get('menu') != 'JEYUK' or job.get('provenance') != 'SYNTHETIC':
                raise RecipeScopeDenied('recipe scope unavailable')
            binding = job.get('source_binding')
            if not isinstance(binding, dict) or binding.get('source_id') != 'synthetic-rest' or \
                    binding.get('device_id') != 'mockprofile-110-a-001' or \
                    not isinstance(binding.get('batch_id'), str) or \
                    not re.fullmatch(r'jeyuk-[A-Za-z0-9-]{1,121}', binding['batch_id']):
                raise RecipeScopeDenied('recipe scope unavailable')
            jobs.append(pk)
            sources.append('JEYUK#' + binding['batch_id'])
        if sources[0] == sources[1]:
            raise RecipeScopeDenied('recipe scope unavailable')
        return RecipeReadScope(subject, execution_id, tuple(jobs), tuple(sources)), versions, revisions

    first = snapshot()
    if snapshot() != first or expires <= time.time():
        raise RecipeScopeDenied('recipe scope changed')
    return first[0]
