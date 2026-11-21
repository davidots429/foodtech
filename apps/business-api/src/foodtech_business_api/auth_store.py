"""Shared, expiring login flows and server-owned sessions."""

import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Protocol

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError


def _check_lease(lease: str, now: int, lease_until: int) -> None:
    if not isinstance(lease, str) or not 16 <= len(lease) <= 128 or type(now) is not int or \
            type(lease_until) is not int or not now < lease_until <= now + 60:
        raise ValueError("invalid refresh lease")


def _check_refresh_result(lease: str, version: int, now: int, access: str, ciphertext: bytes, expires: int) -> None:
    if not isinstance(lease, str) or not 16 <= len(lease) <= 128 or type(version) is not int or version < 0 or \
            type(now) is not int or not isinstance(access, str) or not access or len(access) > 16384 or \
            not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= 65536 or \
            type(expires) is not int or expires <= now:
        raise ValueError("invalid refresh result")


@dataclass
class LoginFlow:
    surface: str
    state: str
    verifier: str
    expires: int


@dataclass
class Session:
    surface: str
    subject: str
    email: str | None
    csrf: str
    expires: int
    epoch: int
    access_token: str | None = None
    refresh_ciphertext: bytes | None = None
    refresh_version: int = 0
    refresh_lock: str | None = None
    refresh_lock_until: int = 0
    refresh_deadline: int = 0
    access_expires: int | None = None


class AuthStore(Protocol):
    def save_flow(self, flow_id: str, flow: LoginFlow) -> None: ...
    def take_flow(self, flow_id: str) -> LoginFlow | None: ...
    def save_session(self, session_id: str, session: Session) -> None: ...
    def read_session(self, session_id: str) -> Session | None: ...
    def delete_session(self, session_id: str) -> None: ...
    def take_session(self, session_id: str) -> Session | None: ...
    def claim_refresh(self, session_id: str, lease: str, now: int, lease_until: int) -> Session | None: ...
    def finish_refresh(self, session_id: str, lease: str, expected_version: int, now: int,
                       access_token: str, ciphertext: bytes, expires: int) -> bool: ...
    def principal(self, subject: str) -> dict: ...
    def block_admin(self, subject: str) -> None: ...
    def suspend_user(self, subject: str) -> None: ...


class MemoryStore:
    """Local development only; never use for a deployed API."""

    def __init__(self) -> None:
        self.flows: dict[str, LoginFlow] = {}
        self.sessions: dict[str, Session] = {}
        self.principals: dict[str, dict] = {}
        self.lock = threading.RLock()

    def save_flow(self, flow_id: str, flow: LoginFlow) -> None:
        with self.lock:
            self.flows[flow_id] = flow

    def take_flow(self, flow_id: str) -> LoginFlow | None:
        with self.lock:
            flow = self.flows.pop(flow_id, None)
            return flow if flow and flow.expires > time.time() else None

    def save_session(self, session_id: str, session: Session) -> None:
        with self.lock:
            self.sessions[session_id] = session

    def read_session(self, session_id: str) -> Session | None:
        with self.lock:
            session = self.sessions.get(session_id)
            return session if session and session.expires > time.time() else None

    def delete_session(self, session_id: str) -> None:
        with self.lock:
            self.sessions.pop(session_id, None)

    def take_session(self, session_id: str) -> Session | None:
        with self.lock:
            return self.sessions.pop(session_id, None)

    def claim_refresh(self, session_id: str, lease: str, now: int, lease_until: int) -> Session | None:
        _check_lease(lease, now, lease_until)
        with self.lock:
            current = self.sessions.get(session_id)
            if not current or current.expires <= now or current.refresh_deadline <= now or \
                    not current.refresh_ciphertext or current.refresh_lock_until > now:
                return None
            current = replace(current, refresh_lock=lease, refresh_lock_until=lease_until)
            self.sessions[session_id] = current
            return replace(current)

    def finish_refresh(self, session_id: str, lease: str, expected_version: int, now: int,
                       access_token: str, ciphertext: bytes, expires: int) -> bool:
        _check_refresh_result(lease, expected_version, now, access_token, ciphertext, expires)
        with self.lock:
            current = self.sessions.get(session_id)
            if not current or current.expires <= now or current.refresh_lock != lease or \
                    current.refresh_lock_until <= now or current.refresh_version != expected_version or \
                    expires > current.refresh_deadline or expires > current.expires:
                return False
            self.sessions[session_id] = replace(current, access_token=access_token, refresh_ciphertext=ciphertext,
                refresh_version=expected_version + 1, refresh_lock=None, refresh_lock_until=0, access_expires=expires)
            return True

    def principal(self, subject: str) -> dict:
        with self.lock:
            return self.principals.get(subject, {"epoch": 0}).copy()

    def block_admin(self, subject: str) -> None:
        with self.lock:
            current = self.principals.setdefault(subject, {"epoch": 0})
            current["epoch"] += 1
            current["admin_blocked"] = True

    def suspend_user(self, subject: str) -> None:
        with self.lock:
            current = self.principals.setdefault(subject, {"epoch": 0})
            current["epoch"] += 1
            current["suspended"] = True


class DynamoAuthStore:
    def __init__(self, table_name: str) -> None:
        import boto3

        self.table = boto3.resource("dynamodb", region_name="ap-southeast-2").Table(table_name)

    def save_flow(self, flow_id: str, flow: LoginFlow) -> None:
        self.table.put_item(Item={"pk": f"FLOW#{flow_id}", **asdict(flow)},
                            ConditionExpression=Attr("pk").not_exists())

    def take_flow(self, flow_id: str) -> LoginFlow | None:
        item = self.table.delete_item(Key={"pk": f"FLOW#{flow_id}"},
                                      ReturnValues="ALL_OLD").get("Attributes")
        return LoginFlow(**{key: item[key] for key in ("surface", "state", "verifier", "expires")}) \
            if item and int(item["expires"]) > time.time() else None

    def save_session(self, session_id: str, session: Session) -> None:
        self.table.put_item(Item={"pk": f"SESSION#{session_id}", **asdict(session)},
                            ConditionExpression=Attr("pk").not_exists())

    def read_session(self, session_id: str) -> Session | None:
        item = self.table.get_item(Key={"pk": f"SESSION#{session_id}"},
                                   ConsistentRead=True).get("Item")
        return self._session(item) \
            if item and int(item["expires"]) > time.time() else None

    @staticmethod
    def _session(item: dict) -> Session:
        cipher = item.get("refresh_ciphertext")
        return Session(**{key: item[key] for key in ("surface", "subject", "email", "csrf", "expires", "epoch")},
            access_token=item.get("access_token"), refresh_ciphertext=bytes(cipher) if cipher else None,
            refresh_version=int(item.get("refresh_version", 0)), refresh_lock=item.get("refresh_lock"),
            refresh_lock_until=int(item.get("refresh_lock_until", 0)), refresh_deadline=int(item.get("refresh_deadline", 0)),
            access_expires=int(item["access_expires"]) if item.get("access_expires") is not None else None)

    def delete_session(self, session_id: str) -> None:
        self.table.delete_item(Key={"pk": f"SESSION#{session_id}"})

    def take_session(self, session_id: str) -> Session | None:
        item = self.table.delete_item(Key={"pk": f"SESSION#{session_id}"}, ReturnValues="ALL_OLD").get("Attributes")
        return self._session(item) if item else None

    def claim_refresh(self, session_id: str, lease: str, now: int, lease_until: int) -> Session | None:
        _check_lease(lease, now, lease_until)
        try:
            item = self.table.update_item(Key={"pk": f"SESSION#{session_id}"},
                UpdateExpression="SET refresh_lock = :lease, refresh_lock_until = :until",
                ConditionExpression=Attr("pk").exists() & Attr("expires").gt(now) &
                    Attr("refresh_deadline").gt(now) & Attr("refresh_ciphertext").attribute_type("B") &
                    (Attr("refresh_lock_until").not_exists() | Attr("refresh_lock_until").lte(now)),
                ExpressionAttributeValues={":lease": lease, ":until": lease_until}, ReturnValues="ALL_NEW")["Attributes"]
            return self._session(item)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise

    def finish_refresh(self, session_id: str, lease: str, expected_version: int, now: int,
                       access_token: str, ciphertext: bytes, expires: int) -> bool:
        _check_refresh_result(lease, expected_version, now, access_token, ciphertext, expires)
        try:
            self.table.update_item(Key={"pk": f"SESSION#{session_id}"},
                UpdateExpression="SET access_token = :access, refresh_ciphertext = :cipher, "
                                 "refresh_version = :next, refresh_lock_until = :zero, access_expires = :expires REMOVE refresh_lock",
                ConditionExpression=Attr("pk").exists() & Attr("expires").gt(now) & Attr("refresh_lock").eq(lease) &
                    Attr("refresh_lock_until").gt(now) & Attr("refresh_version").eq(expected_version) &
                    Attr("refresh_deadline").gte(expires) & Attr("expires").gte(expires),
                ExpressionAttributeValues={":access": access_token, ":cipher": ciphertext,
                    ":next": expected_version + 1, ":zero": 0, ":expires": expires})
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def principal(self, subject: str) -> dict:
        item = self.table.get_item(Key={"pk": f"USER#{subject}"},
                                   ConsistentRead=True).get("Item")
        return item or {"epoch": 0}

    def block_admin(self, subject: str) -> None:
        self.table.update_item(
            Key={"pk": f"USER#{subject}"},
            UpdateExpression="SET admin_blocked = :blocked ADD #epoch :one",
            ExpressionAttributeNames={"#epoch": "epoch"},
            ExpressionAttributeValues={":blocked": True, ":one": 1},
        )

    def suspend_user(self, subject: str) -> None:
        self.table.update_item(
            Key={"pk": f"USER#{subject}"},
            UpdateExpression="SET suspended = :suspended ADD #epoch :one",
            ExpressionAttributeNames={"#epoch": "epoch"},
            ExpressionAttributeValues={":suspended": True, ":one": 1},
        )


@lru_cache(maxsize=4)
def _store(table_name: str) -> AuthStore:
    return DynamoAuthStore(table_name) if table_name else MemoryStore()


def get_store() -> AuthStore:
    return _store(os.getenv("FOODTECH_AUTH_TABLE", ""))
