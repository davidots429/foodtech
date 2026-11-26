"""Pure validation for opaque source-read grant references."""
import re
from uuid import UUID


class SourceGrantConflict(Exception):
    """The observed account/grant mapping is no longer current."""


def _grant_mapping(principal):
    mapping = principal.get('source_read_grants', {})
    if not isinstance(mapping, dict):
        raise SourceGrantConflict('source grant mapping unavailable')
    try:
        if any(not isinstance(key, str) or not re.fullmatch(r'JEYUK#jeyuk-[A-Za-z0-9-]{1,121}', key) or
               not isinstance(ref, str) or str(UUID(ref)) != ref for key, ref in mapping.items()) or \
                len(set(mapping.values())) != len(mapping):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SourceGrantConflict('source grant mapping unavailable') from None
    return dict(mapping)
