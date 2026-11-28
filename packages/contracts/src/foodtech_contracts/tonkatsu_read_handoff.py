"""Opaque STS handoff for a dedicated authenticated fryer listener."""
import hashlib
import hmac
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone

from .tonkatsu_read_scope import TonkatsuReadScope

SCHEMA = "tonkatsu-source-read-handoff-v1"
CONTENT_TYPE = "application/vnd.foodtech.tonkatsu-source-read+json"
MAX_BYTES = 65536
ROLE_PATTERN = r"arn:aws:iam::([0-9]{12}):role/foodtech-tonkatsu-source-read-([a-z0-9-]{1,32})"


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate fryer handoff field")
        result[key] = value
    return result


def scope_sha256(scope):
    if type(scope) is not TonkatsuReadScope:
        raise ValueError("verified fryer scope required")
    raw = json.dumps(asdict(scope), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


class TonkatsuReadHandoff:
    __slots__ = ("scope_digest", "role", "assumed_role", "expires", "__credentials", "_clock", "_last_time")

    def __init__(self, *, scope_digest, role, assumed_role, expires, credentials, clock):
        self.scope_digest, self.role, self.assumed_role, self.expires = scope_digest, role, assumed_role, expires
        self.__credentials = tuple(credentials)
        self._clock, self._last_time = clock, None
        self.validate()

    def __repr__(self):
        return "TonkatsuReadHandoff()"

    def __reduce_ex__(self, protocol):
        raise TypeError("fryer handoffs cannot be serialized")

    def validate(self, scope=None):
        role = re.fullmatch(ROLE_PATTERN, self.role)
        if not role or not re.fullmatch(r"[a-f0-9]{64}", self.scope_digest) or not re.fullmatch(
            rf"arn:aws:sts::{role[1]}:assumed-role/foodtech-tonkatsu-source-read-{role[2]}/tonkatsu-[a-f0-9]{{32}}",
            self.assumed_role,
        ):
            raise ValueError("invalid fryer handoff identity")
        now = self._clock()
        if (type(now) is not datetime or now.utcoffset() is None or type(self.expires) is not datetime
            or self.expires.utcoffset() is None or not 0 < (self.expires - now).total_seconds() <= 930
            or (self._last_time is not None and now < self._last_time)
        ):
            raise ValueError("invalid fryer handoff expiry")
        if scope is not None and (
            not hmac.compare_digest(self.scope_digest, scope_sha256(scope))
            or not scope.history.writer_verified_at <= now.timestamp() < self.expires.timestamp()
            or self.expires.timestamp() > scope.history.writer_valid_until
        ):
            raise ValueError("fryer handoff scope mismatch")
        if len(self.__credentials) != 3 or any(
            type(value) is not str or not 1 <= len(value) <= 16384 or not value.isascii()
            or any(not 33 <= ord(char) <= 126 for char in value) for value in self.__credentials
        ):
            raise ValueError("invalid fryer handoff credentials")
        self._last_time = now

    def live_timestamp(self):
        self.validate()
        return self._last_time.timestamp()

    def _sdk_credentials(self):
        self.validate()
        return dict(zip(("aws_access_key_id", "aws_secret_access_key", "aws_session_token"), self.__credentials))

    def _encode_for_internal_tls(self):
        self.validate()
        raw = json.dumps({
            "schema": SCHEMA, "scope_digest": self.scope_digest, "role": self.role,
            "assumed_role": self.assumed_role, "expires": self.expires.astimezone(timezone.utc).isoformat(),
            "credentials": self.__credentials,
        }, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("fryer handoff exceeds size limit")
        return raw

    @classmethod
    def _decode_from_internal_tls(cls, raw, *, clock):
        try:
            if type(raw) is not bytes or not 1 <= len(raw) <= MAX_BYTES:
                raise ValueError
            value = json.loads(raw, object_pairs_hook=_unique_fields)
            if type(value) is not dict or set(value) != {
                "schema", "scope_digest", "role", "assumed_role", "expires", "credentials"
            } or value.pop("schema") != SCHEMA or type(value["credentials"]) is not list:
                raise ValueError
            value["expires"] = datetime.fromisoformat(value["expires"])
            return cls(**value, clock=clock)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError("fryer handoff unavailable") from None
