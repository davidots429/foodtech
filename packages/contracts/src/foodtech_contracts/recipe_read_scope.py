"""Exact recipe read bounds; validation does not grant AWS access."""
from dataclasses import dataclass, field
import re
from uuid import UUID


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
    auth_epoch: int | None = field(default=None, repr=False)


def validate_recipe_read_scope(scope):
    if type(scope) is not RecipeReadScope or not isinstance(scope.owner, str) or \
            not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', scope.owner):
        raise ValueError('recipe read scope unavailable')
    try:
        if not isinstance(scope.execution_id, str) or str(UUID(scope.execution_id)) != scope.execution_id:
            raise ValueError
        if type(scope.job_partitions) is not tuple or len(scope.job_partitions) != 2 or any(
                not isinstance(pk, str) or not re.fullmatch(r'SCOPE#USER#' + re.escape(scope.owner) +
                    r'#JOB#[0-9a-f-]{36}', pk) or str(UUID(pk.rsplit('#', 1)[1])) != pk.rsplit('#', 1)[1]
                for pk in scope.job_partitions) or scope.job_partitions[0] == scope.job_partitions[1]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('recipe read scope unavailable') from None
    if type(scope.source_partitions) is not tuple or len(scope.source_partitions) != 2 or any(
            not isinstance(pk, str) or not re.fullmatch(r'JEYUK#jeyuk-[A-Za-z0-9-]{1,121}', pk)
            for pk in scope.source_partitions) or scope.source_partitions[0] == scope.source_partitions[1] or \
            any(type(values) is not tuple or len(values) != 2 or any(type(value) is not int or not 1 <= value <= 10**18
                for value in values) for values in (scope.job_versions, scope.source_revisions)) or \
            type(scope.auth_epoch) is not int or not 0 <= scope.auth_epoch < 2**63 or \
            not isinstance(scope.execution_state, str) or scope.execution_state not in {'PENDING', 'RESERVED'} or \
            type(scope.execution_version) is not int or not 1 <= scope.execution_version < 2**63:
        raise ValueError('recipe read scope unavailable')
