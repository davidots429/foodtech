"""Read versioned tombstone metadata from the live deletion registry.

The recovery coordinator supplies a restricted DynamoDB resource and the trusted
live table ARN. A complete traversal is not a point-in-time snapshot while
registry writers are active. The coordinator must establish registry stability
through cutover separately before declaring the restored environment eligible.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from foodtech_contracts.recovery_reconciliation import TOMBSTONE_STATES, Tombstone


_LIVE_REGISTRY_ARN = re.compile(
    r"arn:aws:dynamodb:ap-southeast-2:[0-9]{12}:table/foodtech-deletion-registry-[a-z0-9-]{1,32}"
)


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


class RecoveryRegistryInventory:
    def __init__(self, dynamodb, *, live_registry_arn: str):
        if not isinstance(live_registry_arn, str) or not _LIVE_REGISTRY_ARN.fullmatch(live_registry_arn):
            raise ValueError("explicit live deletion registry ARN required")
        self._table = dynamodb.Table(live_registry_arn)

    def read(self) -> tuple[VersionedTombstone, ...]:
        """Return all retained request records, including requests predating the restore."""
        parameters = {
            "ConsistentRead": True,
            "KeyConditionExpression": "pk = :registry AND begins_with(sk, :prefix)",
            "ProjectionExpression": "pk, sk, request_id, #status, #version",
            "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
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
                request_id = _request_key(item)
                state = item.get("status")
                version = item.get("version")
                if (request_id <= previous or item.get("request_id") != request_id
                        or not isinstance(state, str) or state not in TOMBSTONE_STATES
                        or not isinstance(version, Decimal) or not version.is_finite()
                        or not 1 <= version < 2**63 or version != int(version)):
                    raise RecoveryRegistryInventoryError("invalid or out-of-order deletion registry entry")
                entries.append(VersionedTombstone(Tombstone(request_id, state), int(version)))
                previous = request_id

            cursor = page.get("LastEvaluatedKey", {})
            if cursor == {}:
                return tuple(entries)
            if not items or _request_key(cursor) != previous or set(cursor) != {"pk", "sk"}:
                raise RecoveryRegistryInventoryError("invalid deletion registry pagination cursor")
            parameters["ExclusiveStartKey"] = cursor
