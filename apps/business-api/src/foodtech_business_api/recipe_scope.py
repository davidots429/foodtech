"""Resolve a recipe execution's candidate read scope inside a trusted boundary.

This does not issue credentials, authenticate the caller, or authorize a model
call. Callers must supply a freshly authenticated customer session and trusted,
strongly consistent execution and job readers. A later broker must enforce the
returned bounds in AWS, independently of the gateway and request body.
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from .auth import _session


class RecipeScopeDenied(Exception):
    pass


@dataclass(frozen=True)
class RecipeReadScope:
    owner: str = field(repr=False)
    execution_id: str = field(repr=False)
    job_partitions: tuple[str, str] = field(repr=False)
    source_partitions: tuple[str, str] = field(repr=False)
    job_versions: tuple[int, int] = field(repr=False)
    source_revisions: tuple[int, int] = field(repr=False)
    execution_state: str = field(repr=False)
    execution_version: int = field(repr=False)


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
        execution_snapshot = execution_reader(execution_id)
        execution = getattr(execution_snapshot, 'execution', None)
        state, record_version = getattr(execution_snapshot, 'state', None), getattr(execution_snapshot, 'version', None)
        if not isinstance(state, str) or state not in {'PENDING', 'RESERVED'} or type(record_version) is not int or \
                not 1 <= record_version < 2**63 or not execution or \
                getattr(execution, 'execution_id', None) != execution_id or \
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
        return RecipeReadScope(subject, execution_id, tuple(jobs), tuple(sources), versions, revisions, state, record_version)

    first = snapshot()
    if snapshot() != first or expires <= time.time():
        raise RecipeScopeDenied('recipe scope changed')
    return first


async def authenticated_recipe_scope(request, execution_id, execution_reader, job_reader):
    """Recheck the customer session around scope discovery; do not grant AWS access.

    Readers must be trusted and strongly consistent. The caller must not expose
    this helper as a credential issuer without an independent broker boundary.
    """
    before = await _session(request, 'customer')
    scope = await asyncio.to_thread(resolve_recipe_scope, before, execution_id, execution_reader, job_reader)
    after = await _session(request, 'customer')
    if (before.subject, before.epoch, before.csrf) != (after.subject, after.epoch, after.csrf):
        raise RecipeScopeDenied('customer session changed')
    return scope


async def revalidate_recipe_scope(request, expected, execution_reader, job_reader):
    """Reject a scope whose authenticated owner, execution or source references changed.

    This compares observations and cannot prevent a subsequent concurrent write.
    The eventual caller must enforce authorization and freshness at its boundary.
    """
    if not isinstance(expected, RecipeReadScope):
        raise RecipeScopeDenied('recipe scope unavailable')
    current = await authenticated_recipe_scope(request, expected.execution_id, execution_reader, job_reader)
    if current != expected:
        raise RecipeScopeDenied('recipe scope changed')
    return current
