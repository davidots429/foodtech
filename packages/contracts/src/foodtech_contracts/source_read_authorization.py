"""Observed source-grant versions, not credentials or proof of authorization."""
from dataclasses import dataclass, field
import re
from uuid import UUID


@dataclass(frozen=True)
class SourceReadAuthorization:
    auth_table: str
    catalog_table: str
    subject: str = field(repr=False)
    execution_id: str = field(repr=False)
    auth_epoch: int = field(repr=False)
    source_partitions: tuple[str, str] = field(repr=False)
    grant_ids: tuple[str, str] = field(repr=False)
    grant_versions: tuple[int, int] = field(repr=False)
    expires_at: tuple[int, int] = field(repr=False)

    def __post_init__(self):
        stage = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', self.auth_table) if isinstance(self.auth_table, str) else None
        if not stage or self.catalog_table != 'foodtech-source-access-' + stage[1] or \
                not isinstance(self.subject, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.subject) or \
                not isinstance(self.execution_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.execution_id) or \
                type(self.auth_epoch) is not int or not 0 <= self.auth_epoch < 2**63:
            raise ValueError('invalid source authorization observation')
        if any(type(values) is not tuple or len(values) != 2 for values in
               (self.source_partitions, self.grant_ids, self.grant_versions, self.expires_at)) or \
                any(not isinstance(pk, str) or not re.fullmatch(r'JEYUK#[A-Za-z0-9_-]{1,128}', pk)
                    for pk in self.source_partitions) or self.source_partitions[0] == self.source_partitions[1] or \
                any(type(value) is not int or not 1 <= value < 2**63 for value in (*self.grant_versions, *self.expires_at)):
            raise ValueError('invalid source authorization observation')
        try:
            if any(not isinstance(value, str) or str(UUID(value)) != value for value in self.grant_ids) or \
                    self.grant_ids[0] == self.grant_ids[1]:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid source authorization observation') from None
