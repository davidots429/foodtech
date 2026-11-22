"""Strict nonsecret metadata and encrypted Capsule entry contract."""
import base64
import re
from dataclasses import dataclass, field


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_EPOCH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_PSEUDONYM = re.compile(r"psn:v1:([a-z][a-z0-9_]{0,31}):([0-9]{4}-(?:0[1-9]|1[0-2])):([A-Za-z0-9._-]{1,64}):([A-Za-z0-9_-]{43})")
_KEY = re.compile(r"arn:aws:kms:ap-southeast-2:[0-9]{12}:key/[A-Za-z0-9-]+")


def _digest(value: str) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
        return len(decoded) == 32 and base64.urlsafe_b64encode(decoded).decode().rstrip("=") == value
    except ValueError:
        return False


def validate_tenant(tenant: str, approved_tenant_purposes: frozenset[str]) -> None:
    match = _PSEUDONYM.fullmatch(tenant) if isinstance(tenant, str) else None
    if not match or match.group(1) not in approved_tenant_purposes or \
            int(match.group(2)[:4]) < 1 or not _digest(match.group(4)):
        raise ValueError("invalid tenant pseudonym")


@dataclass(frozen=True)
class Entry:
    purpose: str
    epoch: str
    pseudonym_digest: str = field(repr=False)

    def validate(self, approved_purposes: frozenset[str]) -> None:
        if not isinstance(self.purpose, str) or self.purpose not in approved_purposes or \
                not isinstance(self.epoch, str) or not _EPOCH.fullmatch(self.epoch) or int(self.epoch[:4]) < 1 or \
                not _digest(self.pseudonym_digest):
            raise ValueError("invalid capsule entry")


@dataclass(frozen=True)
class Envelope:
    pool_version: str
    shard_id: str
    key_arn: str
    ciphertext: bytes = field(repr=False)

    def validate(self, key_map: dict[tuple[str, str], str]) -> None:
        if not isinstance(self.pool_version, str) or not _NAME.fullmatch(self.pool_version) or \
                not isinstance(self.shard_id, str) or not _NAME.fullmatch(self.shard_id) or \
                not isinstance(self.key_arn, str) or not _KEY.fullmatch(self.key_arn) or \
                key_map.get((self.pool_version, self.shard_id)) != self.key_arn or \
                not isinstance(self.ciphertext, bytes) or not 1 <= len(self.ciphertext) <= 400_000:
            raise ValueError("invalid capsule envelope")


def context(tenant: str, pool_version: str, shard_id: str, approved_tenant_purposes: frozenset[str]) -> dict[str, str]:
    validate_tenant(tenant, approved_tenant_purposes)
    if not isinstance(pool_version, str) or not _NAME.fullmatch(pool_version) or \
            not isinstance(shard_id, str) or not _NAME.fullmatch(shard_id):
        raise ValueError("invalid capsule key metadata")
    return {"tenant_pseudonym": tenant, "purpose": "CAPSULE_ENTRY",
            "pool_version": pool_version, "shard_id": shard_id}


def header_matches(actual: dict[str, str], expected: dict[str, str]) -> bool:
    """Accept only approved context keys and the SDK's signing public key."""
    return isinstance(actual, dict) and set(actual) <= set(expected) | {"aws-crypto-public-key"} and \
        all(actual.get(key) == value for key, value in expected.items())
