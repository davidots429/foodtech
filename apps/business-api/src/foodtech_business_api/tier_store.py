"""Demo tier applications; the full business number is never persisted."""

import os
import re
import threading
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from fastapi import HTTPException


class TierConflict(Exception):
    pass


class TierStore(Protocol):
    def current(self, subject: str) -> dict | None: ...
    def submit(self, subject: str, item: dict, expected_epoch: int = 0) -> None: ...
    def pending(self, limit: int = 20) -> list[dict]: ...
    def inventory_keys(self, subject: str, limit: int, cursor: str | None) -> dict: ...
    def decide(self, subject: str, application_id: str, reviewer: str,
               decision: str, reviewed_at: str, reviewer_epoch: int = 0) -> dict: ...


def _current(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in {"pk", "sk", "queue_status", "queue_sort"}}


def _inventory_args(subject: str, limit: int, cursor: str | None) -> None:
    if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', subject) or \
            type(limit) is not int or not 1 <= limit <= 100 or (cursor is not None and
            (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048)):
        raise ValueError('invalid tier inventory scope')


class MemoryTierStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.lock = threading.RLock()

    def current(self, subject: str) -> dict | None:
        with self.lock:
            item = self.items.get((f"TENANT#{subject}", "CURRENT"))
            return _current(item) if item else None

    def submit(self, subject: str, item: dict, expected_epoch: int = 0) -> None:
        with self.lock:
            pk = f"TENANT#{subject}"
            old = self.items.get((pk, "CURRENT"))
            if old and old["status"] != "REJECTED":
                raise TierConflict
            current = {"pk": pk, "sk": "CURRENT", **item,
                       "queue_status": "REQUESTED", "queue_sort": item["submitted_at"] + "#" + item["application_id"]}
            self.items[(pk, "CURRENT")] = current
            self.items[(pk, f"APPLICATION#{item['application_id']}")] = {
                "pk": pk, "sk": f"APPLICATION#{item['application_id']}", **item}

    def pending(self, limit: int = 20) -> list[dict]:
        with self.lock:
            values = [item for (_, sk), item in self.items.items()
                      if sk == "CURRENT" and item.get("queue_status") == "REQUESTED"]
            return [_current(item) for item in sorted(values, key=lambda row: row["queue_sort"])[:limit]]

    def inventory_keys(self, subject: str, limit: int, cursor: str | None) -> dict:
        _inventory_args(subject, limit, cursor)
        with self.lock:
            pk = f'TENANT#{subject}'
            keys = sorted(sk for (item_pk, sk) in self.items if item_pk == pk and (cursor is None or sk > cursor))
            page = keys[:limit]
            return {'keys': page, 'next_cursor': page[-1] if len(keys) > limit else None}

    def decide(self, subject: str, application_id: str, reviewer: str,
               decision: str, reviewed_at: str, reviewer_epoch: int = 0) -> dict:
        with self.lock:
            pk = f"TENANT#{subject}"
            old = self.items.get((pk, "CURRENT"))
            if not old or old["application_id"] != application_id or old["status"] != "REQUESTED":
                raise TierConflict
            item = {**old, "status": decision,
                    "tier": "DEMO_PAID" if decision == "APPROVED_FOR_DEMO" else "FREE",
                    "reviewed_at": reviewed_at, "reviewed_by": reviewer,
                    "version": old["version"] + 1}
            item.pop("queue_status", None)
            item.pop("queue_sort", None)
            self.items[(pk, "CURRENT")] = item
            self.items[(pk, f"REVIEW#{application_id}")] = {
                "pk": pk, "sk": f"REVIEW#{application_id}", "application_id": application_id,
                "decision": decision, "reviewed_at": reviewed_at, "reviewed_by": reviewer}
            return _current(item)


class DynamoTierStore:
    def __init__(self, table_name: str) -> None:
        import boto3

        session = boto3.Session(region_name="ap-southeast-2")
        self.table = session.resource("dynamodb").Table(table_name)
        self.client = session.client("dynamodb")
        self.serializer = TypeSerializer()
        self.auth_table_name = os.getenv("FOODTECH_AUTH_TABLE", "")
        self.auth_table = session.resource("dynamodb").Table(self.auth_table_name) if self.auth_table_name else None

    def _item(self, item: dict) -> dict:
        return {key: self.serializer.serialize(value) for key, value in item.items()}

    def _guard(self, subject: str, epoch: int | None, *, admin: bool = False) -> dict:
        if not self.auth_table_name or not isinstance(subject, str) or not subject:
            raise ValueError("authentication table and subject are required")
        condition = "(attribute_not_exists(suspended) OR suspended = :false) AND attribute_not_exists(deletion_lock_request)"
        if admin:
            condition += " AND (attribute_not_exists(admin_blocked) OR admin_blocked = :false)"
        values = {":false": {"BOOL": False}}
        names = {}
        if epoch is not None:
            if type(epoch) is not int or not 0 <= epoch < 2**63:
                raise ValueError("invalid session epoch")
            condition += " AND ((attribute_not_exists(#epoch) AND :expected = :zero) OR #epoch = :expected)"
            names["#epoch"] = "epoch"
            values.update({":expected": {"N": str(epoch)}, ":zero": {"N": "0"}})
        check = {"TableName": self.auth_table_name, "Key": self._item({"pk": f"USER#{subject}"}),
                 "ConditionExpression": condition, "ExpressionAttributeValues": values}
        if names:
            check["ExpressionAttributeNames"] = names
        return {"ConditionCheck": check}

    def current(self, subject: str) -> dict | None:
        item = self.table.get_item(Key={"pk": f"TENANT#{subject}", "sk": "CURRENT"},
                                   ConsistentRead=True).get("Item")
        return _current(item) if item else None

    def submit(self, subject: str, item: dict, expected_epoch: int = 0) -> None:
        pk = f"TENANT#{subject}"
        current = {"pk": pk, "sk": "CURRENT", **item,
                   "queue_status": "REQUESTED", "queue_sort": item["submitted_at"] + "#" + item["application_id"]}
        application = {"pk": pk, "sk": f"APPLICATION#{item['application_id']}", **item}
        try:
            self.client.transact_write_items(TransactItems=[
                self._guard(subject, expected_epoch),
                {"Put": {"TableName": self.table.name, "Item": self._item(current),
                         "ConditionExpression": "attribute_not_exists(pk) OR #status = :rejected",
                         "ExpressionAttributeNames": {"#status": "status"},
                         "ExpressionAttributeValues": {":rejected": {"S": "REJECTED"}}}},
                {"Put": {"TableName": self.table.name, "Item": self._item(application),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise TierConflict from exc
            raise

    def pending(self, limit: int = 20) -> list[dict]:
        if not self.auth_table or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('authentication table and valid limit required')
        results, cursor = [], None
        # Bound work when many deleted or stale index entries are still visible.
        for _ in range(10):
            args = {'IndexName': 'PendingRequests', 'KeyConditionExpression': Key('queue_status').eq('REQUESTED'),
                    'Limit': limit, 'ScanIndexForward': True}
            if cursor is not None:
                args['ExclusiveStartKey'] = cursor
            page = self.table.query(**args)
            for indexed in page.get('Items', []):
                pk, subject = indexed.get('pk'), indexed.get('subject')
                if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', subject) or \
                        pk != f'TENANT#{subject}' or indexed.get('sk') != 'CURRENT':
                    continue
                current = self.table.get_item(Key={'pk': pk, 'sk': 'CURRENT'}, ConsistentRead=True).get('Item')
                if not current or current.get('subject') != subject or current.get('status') != 'REQUESTED' or \
                        current.get('application_id') != indexed.get('application_id'):
                    continue
                principal = self.auth_table.get_item(Key={'pk': f'USER#{subject}'}, ConsistentRead=True).get('Item', {})
                if principal.get('suspended') or 'deletion_lock_request' in principal:
                    continue
                results.append(_current(current))
                if len(results) == limit:
                    return results
            cursor = page.get('LastEvaluatedKey')
            if not cursor:
                break
        return results

    def inventory_keys(self, subject: str, limit: int, cursor: str | None) -> dict:
        """Internal key inventory; the caller must authorize and protect its scope."""
        _inventory_args(subject, limit, cursor)
        pk = f'TENANT#{subject}'
        args = {'KeyConditionExpression': Key('pk').eq(pk), 'ProjectionExpression': 'pk, sk',
                'ConsistentRead': True, 'Limit': limit}
        if cursor is not None:
            args['ExclusiveStartKey'] = {'pk': pk, 'sk': cursor}
        page = self.table.query(**args)
        keys = []
        for row in page.get('Items', []):
            if row.get('pk') != pk or not isinstance(row.get('sk'), str) or not 1 <= len(row['sk']) <= 2048:
                raise ValueError('invalid tier inventory record')
            keys.append(row['sk'])
        next_key = page.get('LastEvaluatedKey')
        if next_key is not None and (not isinstance(next_key, dict) or set(next_key) != {'pk', 'sk'} or
                next_key['pk'] != pk or not isinstance(next_key['sk'], str) or not 1 <= len(next_key['sk']) <= 2048):
            raise ValueError('invalid tier inventory checkpoint')
        return {'keys': keys, 'next_cursor': next_key['sk'] if next_key is not None else None}

    def deletion_index_candidates(self, subject: str, request_id: str, limit: int,
                                  cursor: str | None = None) -> dict:
        """Read one locked page of possible pending-index sort keys.

        Return values and cursors are sensitive internal data. The caller must
        encrypt and persist every page before deleting any source records. A
        completed query is not proof of GSI propagation or absence.
        """
        _inventory_args(subject, limit, cursor)
        self._locked_transaction([self._deletion_guard(subject, request_id)])
        pk = f'TENANT#{subject}'
        args = {'KeyConditionExpression': Key('pk').eq(pk) & Key('sk').begins_with('APPLICATION#'),
                'ProjectionExpression': 'pk, sk, application_id, submitted_at',
                'ConsistentRead': True, 'Limit': limit}
        if cursor is not None:
            if not cursor.startswith('APPLICATION#'):
                raise ValueError('invalid tier application cursor')
            args['ExclusiveStartKey'] = {'pk': pk, 'sk': cursor}
        page = self.table.query(**args)
        items = page.get('Items')
        if not isinstance(items, list):
            raise ValueError('invalid tier application page')
        keys = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('invalid tier application')
            app, submitted = item.get('application_id'), item.get('submitted_at')
            if item.get('pk') != pk or not isinstance(app, str) or not 1 <= len(app) <= 128 or \
                    item.get('sk') != 'APPLICATION#' + app or not isinstance(submitted, str) or \
                    not 1 <= len(submitted) <= 128 or len(submitted + '#' + app) > 2048:
                raise ValueError('invalid tier application')
            keys.append(submitted + '#' + app)
        next_key = page.get('LastEvaluatedKey')
        if next_key is not None and (not isinstance(next_key, dict) or set(next_key) != {'pk', 'sk'} or
                next_key['pk'] != pk or not isinstance(next_key['sk'], str) or
                not next_key['sk'].startswith('APPLICATION#') or len(next_key['sk']) > 2048):
            raise ValueError('invalid tier application checkpoint')
        return {'queue_sorts': keys, 'next_cursor': next_key['sk'] if next_key is not None else None}

    def _deletion_guard(self, subject: str, request_id: str) -> dict:
        _inventory_args(subject, 1, None)
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid deletion request') from None
        if not self.auth_table_name:
            raise ValueError('authentication table required')
        return {'ConditionCheck': {'TableName': self.auth_table_name,
                                   'Key': self._item({'pk': f'USER#{subject}'}),
                                   'ConditionExpression': 'deletion_lock_request = :request',
                                   'ExpressionAttributeValues': self._item({':request': request_id})}}

    def _locked_transaction(self, operations: list[dict]) -> None:
        try:
            self.client.transact_write_items(TransactItems=operations)
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get('Code') == 'ConditionalCheckFailed'
                   for reason in exc.response.get('CancellationReasons', [])):
                raise TierConflict from exc
            raise

    def delete_inventory_key(self, subject: str, request_id: str, sk: str) -> None:
        """Delete one known key under the request lock, without declaring completion."""
        _inventory_args(subject, 1, sk)
        if sk is None:
            raise ValueError('tier key required')
        self._locked_transaction([
            self._deletion_guard(subject, request_id),
            {'Delete': {'TableName': self.table.name,
                        'Key': self._item({'pk': f'TENANT#{subject}', 'sk': sk})}},
        ])

    def delete_inventory_page(self, subject: str, request_id: str, limit: int) -> int:
        """Delete up to limit keys; retry from the first page after any failure.

        Caller authorization, GSI/backup removal and completion belong to the
        deletion workflow. An empty page is only an observation at query time.
        """
        _inventory_args(subject, limit, None)
        self._locked_transaction([self._deletion_guard(subject, request_id)])
        keys = self.inventory_keys(subject, limit, None)['keys']
        for sk in keys:
            self.delete_inventory_key(subject, request_id, sk)
        return len(keys)

    def decide(self, subject: str, application_id: str, reviewer: str,
               decision: str, reviewed_at: str, reviewer_epoch: int = 0) -> dict:
        old = self.current(subject)
        if not old or old["application_id"] != application_id or old["status"] != "REQUESTED":
            raise TierConflict
        pk = f"TENANT#{subject}"
        item = {"pk": pk, "sk": "CURRENT", **old, "status": decision,
                "tier": "DEMO_PAID" if decision == "APPROVED_FOR_DEMO" else "FREE",
                "reviewed_at": reviewed_at, "reviewed_by": reviewer,
                "version": int(old["version"]) + 1}
        review = {"pk": pk, "sk": f"REVIEW#{application_id}",
                  "application_id": application_id, "decision": decision,
                  "reviewed_at": reviewed_at, "reviewed_by": reviewer}
        try:
            self.client.transact_write_items(TransactItems=[
                *([self._guard(subject, reviewer_epoch, admin=True)] if subject == reviewer else
                  [self._guard(subject, None), self._guard(reviewer, reviewer_epoch, admin=True)]),
                {"Put": {"TableName": self.table.name, "Item": self._item(item),
                         "ConditionExpression": "#status = :requested AND application_id = :application AND #version = :version",
                         "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                         "ExpressionAttributeValues": {":requested": {"S": "REQUESTED"},
                                                       ":application": {"S": application_id},
                                                       ":version": {"N": str(old["version"])}}}},
                {"Put": {"TableName": self.table.name, "Item": self._item(review),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ])
        except self.client.exceptions.TransactionCanceledException as exc:
            if any(reason.get("Code") == "ConditionalCheckFailed"
                   for reason in exc.response.get("CancellationReasons", [])):
                raise TierConflict from exc
            raise
        return _current(item)


@lru_cache(maxsize=4)
def _store(table_name: str) -> TierStore:
    return DynamoTierStore(table_name) if table_name else MemoryTierStore()


def get_tier_store() -> TierStore:
    table_name = os.getenv("FOODTECH_TIER_TABLE", "")
    if os.getenv("FOODTECH_SECURE_COOKIES", "true").lower() == "true" and not table_name:
        raise HTTPException(503, "시연용 티어 저장소가 설정되지 않았습니다.")
    return _store(table_name)
