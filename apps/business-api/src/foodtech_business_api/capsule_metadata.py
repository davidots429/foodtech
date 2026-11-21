"""Validate nonsecret key-pool metadata without granting cryptographic access."""
import re
from uuid import UUID


def validate_key_metadata(pool_version: str, shard_id: str, key_arn: str) -> None:
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value)
           for value in (pool_version, shard_id)) or not isinstance(key_arn, str) or not re.fullmatch(
               r"arn:aws:kms:ap-southeast-2:[0-9]{12}:key/[A-Za-z0-9-]+", key_arn):
        raise ValueError("invalid capsule key metadata")


def validate_user_key_id(key_id: str) -> None:
    try:
        if str(UUID(key_id)) != key_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid capsule user key ID") from None
