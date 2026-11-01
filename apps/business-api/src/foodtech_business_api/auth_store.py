"""Shared, expiring login flows and server-owned sessions."""

import os
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Protocol

from boto3.dynamodb.conditions import Attr


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


class AuthStore(Protocol):
    def save_flow(self, flow_id: str, flow: LoginFlow) -> None: ...
    def take_flow(self, flow_id: str) -> LoginFlow | None: ...
    def save_session(self, session_id: str, session: Session) -> None: ...
    def read_session(self, session_id: str) -> Session | None: ...
    def delete_session(self, session_id: str) -> None: ...
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
        return Session(**{key: item[key] for key in ("surface", "subject", "email", "csrf", "expires", "epoch")}) \
            if item and int(item["expires"]) > time.time() else None

    def delete_session(self, session_id: str) -> None:
        self.table.delete_item(Key={"pk": f"SESSION#{session_id}"})

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
