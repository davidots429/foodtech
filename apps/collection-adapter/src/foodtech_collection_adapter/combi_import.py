"""Attach a bounded, validated synthetic HTTPS Mock stream to a customer meal."""

import argparse
import asyncio
import os
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from foodtech_contracts.combi_stream import CombiStreamValidator
from .rest import RestMockClient


async def import_meal(mock: RestMockClient, api: httpx.AsyncClient, meal_id: str,
                      device_id: str, csrf: str, *, timeout_seconds: float = 5100, publish_live: bool = False) -> dict:
    """Read a complete Mock stream and submit it using the caller's customer session."""
    meal_id = str(UUID(meal_id))
    path = f"/api/customer/combi-meals/{meal_id}"
    response = await api.get(path)
    response.raise_for_status()
    meal = response.json()["meal"]
    if meal["meal_batch_id"] != meal_id or meal.get("source"):
        raise ValueError("meal is not available for source import")
    saved = []
    if publish_live:
        response = await api.get(path + "/source-live/records")
        response.raise_for_status()
        snapshot = response.json()
        saved = snapshot.get("records")
        if (not isinstance(saved, list) or any(not isinstance(line, str) for line in saved) or
                snapshot.get("count") != len(saved) or len(saved) > 5000 or
                (saved and snapshot.get("device_id") != device_id)):
            raise ValueError("live source snapshot is invalid or belongs to a different device")
    if meal["status"] != "PREPARING_RICE" and not saved:
        raise ValueError("meal is not available for a new source import")
    jobs = {"RICE": meal["rice_cooking_job_id"], "TRANSITION": meal["mode_transition_id"],
            "MACKEREL": meal["mackerel_cooking_job_id"]}
    validator = CombiStreamValidator(meal_id, device_id, jobs)
    if timeout_seconds <= 0:
        raise ValueError("invalid stream timeout")
    deadline = time.monotonic() + timeout_seconds
    records, counts, cursor, size = [], {key: 0 for key in jobs}, 0, 0
    while True:
        result = await asyncio.to_thread(mock.poll, cursor, wait_seconds=4) if publish_live else \
                 await asyncio.to_thread(mock.poll, cursor)
        if result.next_cursor != cursor + len(result.records):
            raise ValueError("Mock cursor does not match returned records")
        if not result.records:
            if result.complete:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Mock stream did not complete before the deadline")
            await asyncio.sleep(min(0.1 if publish_live else 5, max(0, deadline - time.monotonic())))
            continue
        pending = []
        for line in result.records:
            length = len(line.encode("utf-8"))
            size += length + 1
            if length > 2048 or size > 2_500_000 or len(records) >= 5000:
                raise ValueError("Mock stream exceeds import limits")
            item = validator.accept(line)
            position = len(records)
            if position < len(saved) and line != saved[position]:
                raise ValueError("Mock source differs from the saved live snapshot")
            counts[item["phase"]] += 1
            records.append(line)
            if position >= len(saved):
                pending.append(line)
        if publish_live:
            for offset in range(0, len(pending), 5):
                chunk = pending[offset:offset + 5]
                expected = max(cursor, len(saved)) + offset
                body = {"device_id": device_id, "expected_cursor": expected, "records": chunk}
                for attempt in range(2):
                    try:
                        response = await api.post(path + "/source-live", headers={"X-CSRF-Token": csrf}, json=body)
                        break
                    except httpx.TransportError:
                        if attempt:
                            raise
                response.raise_for_status()
                if response.json().get("next_cursor") != expected + len(chunk):
                    raise ValueError("live source cursor does not match submitted records")
        cursor = result.next_cursor
    if len(records) < len(saved):
        raise ValueError("Mock source ended before the saved live snapshot")
    if any(count == 0 for count in counts.values()):
        raise ValueError("all three phases must be present")
    response = await api.get(path)
    response.raise_for_status()
    current = response.json()["meal"]
    if (current["meal_batch_id"] != meal_id or current.get("source") or
            any(current[key] != meal[key] for key in ("rice_cooking_job_id", "mode_transition_id", "mackerel_cooking_job_id"))):
        raise ValueError("meal binding changed during source import")
    response = await api.post(path + "/source-observations", headers={"X-CSRF-Token": csrf},
                              json={"expected_version": current["version"], "device_id": device_id,
                                    "records": records})
    response.raise_for_status()
    return response.json()["meal"]["source"]


def _api_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or not parsed.hostname or (parsed.scheme != "https" and not
            (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")))):
        raise ValueError("business API requires HTTPS or a loopback HTTP origin")
    return value.rstrip("/")


async def _keep_session_alive(api: httpx.AsyncClient, csrf: str, expires_in: int) -> None:
    """Refresh the server-owned session while a long synthetic import runs."""
    conflicts = 0
    while True:
        if type(expires_in) is not int or expires_in <= 0:
            raise ValueError("invalid session expiry")
        await asyncio.sleep(max(10, expires_in - 300))
        response = await api.post("/api/customer/session/refresh", headers={"X-CSRF-Token": csrf})
        if response.status_code == 409:
            # Another client is refreshing the same server session.
            conflicts += 1
            if conflicts >= 6:
                raise RuntimeError("session refresh repeatedly conflicted; sign in again")
            expires_in = 310
            continue
        response.raise_for_status()
        conflicts = 0
        expires_in = response.json().get("expires_in")


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Import a synthetic combi stream into a customer meal")
    parser.add_argument("--meal-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--live", action="store_true", help="publish each received window for customer observation")
    args = parser.parse_args()
    api_url = _api_url(os.environ["FOODTECH_BUSINESS_API_URL"])
    session = os.environ["FOODTECH_CUSTOMER_SESSION"]
    if not session:
        raise ValueError("customer session is required")
    mock = RestMockClient(os.environ["FOODTECH_MOCK_BASE_URL"], os.environ["FOODTECH_MOCK_CLIENT_ID"],
                          os.environ["FOODTECH_MOCK_CLIENT_SECRET"], os.environ["FOODTECH_MOCK_CA_FILE"],
                          profile_id="mockprofile-120")
    try:
        async with httpx.AsyncClient(base_url=api_url, cookies={"ft_session": session}, timeout=30) as api:
            me = await api.get("/api/customer/me")
            me.raise_for_status()
            identity = me.json()
            work = asyncio.create_task(import_meal(mock, api, args.meal_id, args.device_id,
                                                   identity["csrf"], publish_live=args.live))
            keepalive = asyncio.create_task(_keep_session_alive(api, identity["csrf"], identity["expires_in"])) \
                if identity.get("refresh_available") else None
            try:
                if keepalive:
                    done, _ = await asyncio.wait({work, keepalive}, return_when=asyncio.FIRST_COMPLETED)
                    if keepalive in done:
                        keepalive.result()
                        raise RuntimeError("session refresh stopped unexpectedly")
                source = await work
            finally:
                for task in (work, keepalive):
                    if task and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (work, keepalive) if task), return_exceptions=True)
            print(f"Imported {source['record_count']} synthetic records; SHA-256: {source['raw_sha256']}")
    finally:
        mock.close()


if __name__ == "__main__":
    asyncio.run(_main())
