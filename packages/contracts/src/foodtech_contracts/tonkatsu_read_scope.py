"""Verified fryer read scope shared by issuer and receiver."""
from dataclasses import dataclass

from .combi_history_witness import canonical_uuid, history_integer
from .tonkatsu_execution_binding import TonkatsuExecutionBinding
from .tonkatsu_prior_history import TonkatsuPriorHistoryWitness


@dataclass(frozen=True, repr=False)
class TonkatsuReadScope:
    execution_id: str
    execution_version: int
    binding: TonkatsuExecutionBinding
    history: TonkatsuPriorHistoryWitness

    def __post_init__(self):
        if not canonical_uuid(self.execution_id) or type(self.execution_version) is not int or (
            type(self.binding) is not TonkatsuExecutionBinding or type(self.history) is not TonkatsuPriorHistoryWitness
        ):
            raise ValueError("typed fryer scope required")
        history_integer(self.execution_version, 1, 2**63 - 1)
        observed = self.history.history
        if (observed.current_job_id, observed.prior_job_id, observed.job_versions, observed.auth_epoch) != (
            self.binding.current.job_id, self.binding.prior.job_id,
            (self.binding.current.job_version, self.binding.prior.job_version), self.binding.auth_epoch,
        ):
            raise ValueError("fryer scope and history mismatch")

    @property
    def job_partitions(self):
        owner = self.history.history.owner
        return tuple(f"SCOPE#USER#{owner}#JOB#{job_id}" for job_id in
                     (self.binding.current.job_id, self.binding.prior.job_id))
