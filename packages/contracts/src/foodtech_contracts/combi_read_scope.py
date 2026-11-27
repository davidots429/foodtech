"""Private Combi read bounds, derived from stored execution and protected history.

These objects do not issue credentials or attest an AWS session policy. Meal
records and raw chunks occupy different partitions; neither the owner roster,
live stream nor another meal belongs to this source-read scope.
"""
from dataclasses import dataclass, field

from .combi_execution_binding import CombiExecutionBinding, validate_combi_execution
from .combi_history_witness import CombiHistoryWitness, canonical_uuid


@dataclass(frozen=True, repr=False)
class CombiReadScope:
    owner: str
    execution_id: str
    binding: CombiExecutionBinding
    history: CombiHistoryWitness = field(repr=False)
    execution_state: str
    execution_version: int

    def __post_init__(self):
        if type(self.history) is not CombiHistoryWitness or type(self.binding) is not CombiExecutionBinding or type(self.owner) is not str or \
                self.owner != self.history.owner or self.binding != self.history.binding or \
                not canonical_uuid(self.execution_id) or type(self.execution_state) is not str or \
                self.execution_state not in ('PENDING', 'RESERVED') or type(self.execution_version) is not int or \
                not 1 <= self.execution_version < 2**63:
            raise ValueError('verified combi read scope required')

    def __repr__(self):
        return 'CombiReadScope()'

    @property
    def meal_partitions(self):
        return tuple(f'SCOPE#USER#{self.owner}#COMBI_MEAL#{meal}' for meal in
                     (self.binding.current_meal_id, self.binding.prior_meal_id))

    @property
    def source_partitions(self):
        return tuple(pk + '#SOURCE' for pk in self.meal_partitions)

    @property
    def read_partitions(self):
        return (*self.meal_partitions, *self.source_partitions)

    def matches_execution(self, execution):
        try:
            validate_combi_execution(execution)
            return execution.execution_id == self.execution_id and execution.tenant_id == self.owner and \
                execution.combi_binding == self.binding
        except (ValueError, TypeError, AttributeError):
            return False

    def matches_snapshot(self, snapshot):
        return self.matches_execution(getattr(snapshot, 'execution', None)) and \
            type(getattr(snapshot, 'version', None)) is int and snapshot.version == self.execution_version and \
            type(getattr(snapshot, 'state', None)) is str and snapshot.state == self.execution_state
