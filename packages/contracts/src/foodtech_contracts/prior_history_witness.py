"""Private history observation; validation is not attestation issuance or IAM."""
import re
from dataclasses import dataclass, field
from uuid import UUID

HISTORY_CONTRACT = 'atomic-cooking-history-v1'


@dataclass(frozen=True)
class PriorHistoryWitness:
    owner: str = field(repr=False)
    current_job_id: str = field(repr=False)
    prior_job_id: str = field(repr=False)
    job_versions: tuple[int, int]
    auth_epoch: int
    history_revision: int
    writer_version: int
    coverage_version: int
    generation: str = field(repr=False)
    roster_sha256: str = field(repr=False)
    writer_manifest_sha256: str = field(repr=False)
    coverage_revision: int
    baseline_roster_sha256: str = field(repr=False)
    coverage_proof_sha256: str = field(repr=False)

    def __post_init__(self):
        try:
            if not isinstance(self.owner, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', self.owner) or \
                    any(not isinstance(value, str) or str(UUID(value)) != value for value in
                        (self.current_job_id, self.prior_job_id, self.generation)) or self.current_job_id == self.prior_job_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid private prior-history identity') from None
        if type(self.job_versions) is not tuple or len(self.job_versions) != 2 or \
                any(type(value) is not int or not 1 <= value <= 10**18 for value in
                    (*self.job_versions, self.history_revision, self.coverage_revision)) or \
                self.coverage_revision > self.history_revision or \
                (self.coverage_revision == self.history_revision and self.baseline_roster_sha256 != self.roster_sha256) or \
                type(self.auth_epoch) is not int or not 0 <= self.auth_epoch < 2**63 or \
                any(type(value) is not int or not 1 <= value < 2**63 for value in (self.writer_version, self.coverage_version)) or \
                any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value) for value in
                    (self.roster_sha256, self.writer_manifest_sha256, self.baseline_roster_sha256, self.coverage_proof_sha256)):
            raise ValueError('invalid private prior-history observation')
