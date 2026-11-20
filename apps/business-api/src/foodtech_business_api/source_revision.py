"""Consistent source revision reads for recommendation evidence, never authorization."""
import asyncio
from decimal import Decimal
from functools import lru_cache

import boto3


@lru_cache(maxsize=4)
def _table(table_name: str):
    return boto3.Session(region_name="ap-southeast-2").resource("dynamodb").Table(table_name)


def _read(table_name: str, stream_pk: str) -> int | None:
    item = _table(table_name).get_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"},
                                      ConsistentRead=True).get("Item")
    if item is None:
        return None
    revision = item.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, (int, Decimal)) or \
            (isinstance(revision, Decimal) and not revision.is_finite()) or \
            not 1 <= revision <= 10**18 or int(revision) != revision:
        raise ValueError("invalid source revision")
    return int(revision)


async def read_source_revision(table_name: str, stream_pk: str) -> int | None:
    return await asyncio.to_thread(_read, table_name, stream_pk)
