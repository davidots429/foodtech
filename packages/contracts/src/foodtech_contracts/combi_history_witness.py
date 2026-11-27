"""Private recorded ordering observation; not an attestation issuer or read grant."""
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import re
from uuid import UUID

from .combi_execution_binding import CombiExecutionBinding

HISTORY_CONTRACT = 'atomic-combi-history-v1'
COVERAGE_SCOPE = 'ALL_RETAINED_COMBI_MEALS'
CONTROL_KEY = 'CONTROL#COMBI_HISTORY_WRITERS'
COVERAGE_FIELD = 'combi_history_coverage'
SIZES = {'MEDIUM_FIXED_RACK_10', 'LARGE_ROLL_IN_RACK_20'}


def history_integer(value, minimum=1, maximum=10**18):
    if type(value) not in (int, Decimal) or (type(value) is Decimal and not value.is_finite()) or \
            not minimum <= value <= maximum or value != int(value):
        raise ValueError('invalid bounded combi history integer')
    return int(value)


def canonical_uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def roster_digest(meal_ids):
    if type(meal_ids) is not list or any(not canonical_uuid(x) for x in meal_ids) or len(set(meal_ids)) != len(meal_ids):
        raise ValueError('canonical unique meal roster required')
    # Sorted arrays of canonical ASCII UUID strings have the same bytes as JCS.
    return hashlib.sha256(json.dumps(sorted(meal_ids), separators=(',', ':')).encode('ascii')).hexdigest()


@dataclass(frozen=True)
class CombiHistoryWitness:
    owner: str = field(repr=False)
    binding: CombiExecutionBinding = field(repr=False)
    device_id: str = field(repr=False)
    equipment_size: str
    history_revision: int
    writer_version: int
    coverage_version: int
    generation: str = field(repr=False)
    roster_sha256: str = field(repr=False)
    writer_manifest_sha256: str = field(repr=False)
    coverage_revision: int
    baseline_roster_sha256: str = field(repr=False)
    coverage_proof_sha256: str = field(repr=False)
    writer_verified_at: int
    writer_valid_until: int

    def __post_init__(self):
        if type(self.owner) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', self.owner) or \
                type(self.binding) is not CombiExecutionBinding or type(self.device_id) is not str or \
                not 1 <= len(self.device_id) <= 128 or type(self.equipment_size) is not str or \
                self.equipment_size not in SIZES or not canonical_uuid(self.generation):
            raise ValueError('invalid combi history identity')
        for value in (self.history_revision, self.writer_version, self.coverage_version):
            if type(value) is not int or not 1 <= value <= 10**18:
                raise ValueError('invalid combi history revision')
        if type(self.coverage_revision) is not int or not 0 <= self.coverage_revision <= self.history_revision or \
                (self.coverage_revision == 0 and self.baseline_roster_sha256 != roster_digest([])) or \
                (self.coverage_revision == self.history_revision and self.baseline_roster_sha256 != self.roster_sha256) or \
                any(type(v) is not str or not re.fullmatch('[a-f0-9]{64}', v) for v in
                    (self.roster_sha256, self.writer_manifest_sha256, self.baseline_roster_sha256, self.coverage_proof_sha256)) or \
                any(type(v) is not int or not 0 < v < 2**63 for v in (self.writer_verified_at, self.writer_valid_until)) or \
                self.writer_valid_until <= self.writer_verified_at:
            raise ValueError('invalid protected combi history proof references')
