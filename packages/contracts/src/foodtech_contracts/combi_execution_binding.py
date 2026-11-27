"""Immutable references to two recorded combi meals, never a read/history grant."""
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
import re
from uuid import UUID


@dataclass(frozen=True, repr=False)
class CombiExecutionBinding:
    menu: str
    current_meal_id: str
    current_meal_version: int
    current_job_id: str
    current_source_sha256: str
    prior_meal_id: str
    prior_meal_version: int
    prior_job_id: str
    prior_source_sha256: str
    auth_epoch: int
    schema_version: str = 'combi-execution-binding-v1'

    def __post_init__(self):
        try:
            if self.menu not in ('RICE', 'MACKEREL') or self.schema_version != 'combi-execution-binding-v1' or \
                    any(type(value) is not str or str(UUID(value)) != value for value in
                        (self.current_meal_id, self.prior_meal_id, self.current_job_id, self.prior_job_id)) or \
                    self.current_meal_id == self.prior_meal_id or self.current_job_id == self.prior_job_id or \
                    any(type(value) is not int or not 1 <= value <= 10**18 for value in
                        (self.current_meal_version, self.prior_meal_version)) or \
                    any(type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value) for value in
                        (self.current_source_sha256, self.prior_source_sha256)) or \
                    self.current_source_sha256 == self.prior_source_sha256:
                raise ValueError
            if type(self.auth_epoch) is not int or not 0 <= self.auth_epoch < 2**63:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('invalid combi execution binding') from None


def parse_combi_binding(payload):
    """Decode wire/resource records; DynamoDB integral Decimal is normalized."""
    if payload is None:
        return None
    if type(payload) is not dict or set(payload) != {field.name for field in fields(CombiExecutionBinding)}:
        raise ValueError('invalid combi execution binding fields')
    values = dict(payload)
    for key in ('current_meal_version', 'prior_meal_version'):
        value = values[key]
        if type(value) not in (int, Decimal) or (type(value) is Decimal and not value.is_finite()) or \
                not 1 <= value <= 10**18 or value != int(value):
            raise ValueError('invalid combi meal version')
        values[key] = int(value)
    epoch = values['auth_epoch']
    if type(epoch) not in (int, Decimal) or (type(epoch) is Decimal and not epoch.is_finite()) or \
            not 0 <= epoch < 2**63 or epoch != int(epoch):
        raise ValueError('invalid combi authentication epoch')
    values['auth_epoch'] = int(epoch)
    return CombiExecutionBinding(**values)


def combi_binding_payload(binding):
    if binding is None:
        return None
    if type(binding) is not CombiExecutionBinding:
        raise ValueError('typed combi execution binding required')
    return asdict(binding)


def validate_combi_execution(execution):
    binding = execution.combi_binding
    if binding is None:
        return
    if type(binding) is not CombiExecutionBinding or execution.feature != 'RECIPE_CORRECTION' or \
            execution.data_classification != 'SYNTHETIC' or execution.comparison_id is not None or \
            execution.jeyuk_source_revision is not None or execution.prior_source_revision is not None or \
            type(execution.job_version) is not int or type(execution.prior_job_version) is not int or \
            (execution.job_id, execution.job_version, execution.prior_job_id, execution.prior_job_version) != \
            (binding.current_job_id, binding.current_meal_version, binding.prior_job_id, binding.prior_meal_version):
        raise ValueError('combi and execution references differ')
