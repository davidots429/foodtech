"""Read versioned tombstone metadata from the live deletion registry.

The recovery coordinator supplies a restricted DynamoDB resource and the trusted
live table ARN. A complete traversal is not a point-in-time snapshot while
registry writers are active. The coordinator must establish registry stability
through cutover separately before declaring the restored environment eligible.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from boto3.dynamodb.types import Binary

from foodtech_contracts.recovery_reconciliation import TOMBSTONE_STATES, Tombstone


_LIVE_REGISTRY_ARN = re.compile(
    r"arn:aws:dynamodb:ap-southeast-2:[0-9]{12}:table/foodtech-deletion-registry-[a-z0-9-]{1,32}"
)
_METADATA_FIELDS = "pk, sk, request_id, #status, #version"
_METADATA_NAMES = {"#status": "status", "#version": "version"}


class RecoveryRegistryInventoryError(ValueError):
    """The registry response cannot be used as complete inventory evidence."""


@dataclass(frozen=True)
class VersionedTombstone:
    tombstone: Tombstone
    version: int


def _request_key(row: object) -> str:
    if not isinstance(row, dict) or row.get("pk") != "REGISTRY":
        raise RecoveryRegistryInventoryError("invalid deletion registry partition")
    sort_key = row.get("sk")
    if not isinstance(sort_key, str) or not sort_key.startswith("REQ#"):
        raise RecoveryRegistryInventoryError("invalid deletion registry sort key")
    request_id = sort_key.removeprefix("REQ#")
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except ValueError:
        raise RecoveryRegistryInventoryError("invalid deletion registry request identifier") from None
    return request_id


def _entry(row: dict | None) -> VersionedTombstone:
    if row is None:
        raise RecoveryRegistryInventoryError("deletion registry entry unavailable")
    request_id = _request_key(row)
    state = row.get("status")
    version = row.get("version")
    if (row.get("request_id") != request_id or not isinstance(state, str) or state not in TOMBSTONE_STATES
            or not isinstance(version, Decimal) or not version.is_finite()
            or not 1 <= version < 2**63 or version != int(version)):
        raise RecoveryRegistryInventoryError("invalid deletion registry entry")
    return VersionedTombstone(Tombstone(request_id, state), int(version))


def _live_table(dynamodb, arn: str):
    if not isinstance(arn, str) or not _LIVE_REGISTRY_ARN.fullmatch(arn):
        raise ValueError("explicit live deletion registry ARN required")
    return dynamodb.Table(arn)


class RecoveryRegistryInventory:
    def __init__(self, dynamodb, *, live_registry_arn: str):
        self._table = _live_table(dynamodb, live_registry_arn)

    def read(self) -> tuple[VersionedTombstone, ...]:
        """Return all retained request records, including requests predating the restore."""
        parameters = {
            "ConsistentRead": True,
            "KeyConditionExpression": "pk = :registry AND begins_with(sk, :prefix)",
            "ProjectionExpression": _METADATA_FIELDS,
            "ExpressionAttributeNames": _METADATA_NAMES,
            "ExpressionAttributeValues": {":registry": "REGISTRY", ":prefix": "REQ#"},
        }
        entries: list[VersionedTombstone] = []
        previous = ""
        while True:
            page = self._table.query(**parameters)
            items = page.get("Items")
            count = page.get("Count")
            if not isinstance(items, list) or type(count) is not int or count != len(items):
                raise RecoveryRegistryInventoryError("incomplete deletion registry page")
            for item in items:
                entry = _entry(item)
                request_id = entry.tombstone.request_id
                if request_id <= previous:
                    raise RecoveryRegistryInventoryError("out-of-order deletion registry entry")
                entries.append(entry)
                previous = request_id

            cursor = page.get("LastEvaluatedKey", {})
            if cursor == {}:
                return tuple(entries)
            if not items or _request_key(cursor) != previous or set(cursor) != {"pk", "sk"}:
                raise RecoveryRegistryInventoryError("invalid deletion registry pagination cursor")
            parameters["ExclusiveStartKey"] = cursor


class RecoveryRegistryLocators:
    """Use encrypted locators under the recovery worker's separate read/decrypt role."""

    def __init__(self, dynamodb, *, live_registry_arn: str, recovery_cipher):
        self._table = _live_table(dynamodb, live_registry_arn)
        self._cipher = recovery_cipher

    def use(self, expected: VersionedTombstone, action: Callable[[bytearray, bytearray], None]) -> None:
        """Hand recovered buffers to internal work after a strongly consistent version check.

        The action must secure its own mutation with the current restore hold or
        another transactional write guard. The read does not hold the Registry
        row stable after the action begins.
        """
        if not callable(action):
            raise ValueError("explicit recovery action required")
        request_id = expected.tombstone.request_id
        key = {"pk": "REGISTRY", "sk": f"REQ#{request_id}"}
        _request_key(key)

        def read(fields):
            return self._table.get_item(
                Key=key, ConsistentRead=True, ProjectionExpression=fields,
                ExpressionAttributeNames=_METADATA_NAMES,
            ).get("Item")

        row = read(f"{_METADATA_FIELDS}, encrypted_user_id, encrypted_capsule_locator")
        if _entry(row) != expected:
            raise RecoveryRegistryInventoryError("deletion registry entry changed before recovery")

        def ciphertext(field):
            value = row.get(field)
            if isinstance(value, Binary):
                value = value.value
            if not isinstance(value, bytes) or not 1 <= len(value) <= 65536:
                raise RecoveryRegistryInventoryError("invalid deletion registry ciphertext")
            return value

        def confirm(user, locator):
            if _entry(read(_METADATA_FIELDS)) != expected:
                raise RecoveryRegistryInventoryError("deletion registry entry changed during recovery")
            action(user, locator)

        self._cipher.use(
            request_id=request_id,
            encrypted_user_id=ciphertext("encrypted_user_id"),
            encrypted_capsule_locator=ciphertext("encrypted_capsule_locator"),
            callback=confirm,
        )
