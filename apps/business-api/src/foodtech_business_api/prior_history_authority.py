"""Compatibility imports for business callers of the shared history reader."""
from foodtech_contracts.dynamo_prior_history import (
    CONTRACT as CONTRACT, DynamoPriorHistoryAuthority as DynamoPriorHistoryAuthority,
    PriorHistoryUnavailable as PriorHistoryUnavailable, _uuid as _uuid,
)
