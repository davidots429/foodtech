"""Attach a bounded, validated synthetic HTTPS Mock stream to a customer meal."""

import argparse
import asyncio
import os
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from foodtech_contracts.combi_stream import CombiStreamValidator
from .rest import RestMockClient


async def import_meal(mock: RestMockClient, api: httpx.AsyncClient, meal_id: str,
                      device_id: str, csrf: str) -> dict:
    """Read a complete Mock stream and submit it using the caller's customer session."""
    meal_id = str(UUID(meal_id))
    path = f"/api/customer/combi-meals/{meal_id}"
    response = await api.get(path)
    response.raise_for_status()
    meal = response.json()["meal"]
    if meal["meal_batch_id"] != meal_id or meal["status"] != "PREPARING_RICE" or meal.get("source"):
        raise ValueError("meal is not available for source import")
    jobs = {"RICE": meal["rice_cooking_job_id"], "TRANSITION": meal["mode_transition_id"],
            "MACKEREL": meal["mackerel_cooking_job_id"]}
    validator = CombiStreamValidator(meal_id, device_id, jobs)
    records, counts, cursor, size = [], {key: 0 for key in jobs}, 0, 0
    while True:
        result = await asyncio.to_thread(mock.poll, cursor)
        if result.next_cursor != cursor + len(result.records):
            raise ValueError("Mock cursor does not match returned records")
        if not result.records:
            break
        for line in result.records:
            length = len(line.encode("utf-8"))
            size += length + 1
            if length > 2048 or size > 2_500_000 or len(records) >= 5000:
                raise ValueError("Mock stream exceeds import limits")
            item = validator.accept(line)
            counts[item["phase"]] += 1
            records.append(line)
        cursor = result.next_cursor
    if any(count == 0 for count in counts.values()):
        raise ValueError("all three phases must be present")
    response = await api.post(path + "/source-observations", headers={"X-CSRF-Token": csrf},
                              json={"expected_version": meal["version"], "device_id": device_id,
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


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Import a synthetic combi stream into a customer meal")
    parser.add_argument("--meal-id", required=True)
    parser.add_argument("--device-id", required=True)
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
            source = await import_meal(mock, api, args.meal_id, args.device_id, me.json()["csrf"])
            print(f"Imported {source['record_count']} synthetic records; SHA-256: {source['raw_sha256']}")
    finally:
        mock.close()


if __name__ == "__main__":
    asyncio.run(_main())
