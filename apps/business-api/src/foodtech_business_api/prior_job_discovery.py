"""Find a bounded prior-job candidate without claiming complete history."""
import asyncio
from uuid import UUID

from fastapi import HTTPException

from .cooking import _pk, _read, _store_call
from .recommendation_evidence import _instant

MAX_LISTED_JOBS = 100
PAGE_SIZE = 50


async def _roster(store, subject: str) -> list[str] | None:
    ids, cursor, seen_cursors = [], None, set()
    while True:
        page = await _store_call(store, store.list_jobs, subject, PAGE_SIZE, cursor)
        for item in page.get("items", []):
            try:
                job_id = item["job_id"]
                if str(UUID(job_id)) != job_id or job_id in ids:
                    return None
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
            ids.append(job_id)
            if len(ids) > MAX_LISTED_JOBS:
                return None
        cursor = page.get("next_cursor")
        if not cursor:
            return ids
        if cursor in seen_cursors:
            return None
        seen_cursors.add(cursor)


async def discover(request, job_id: str) -> dict:
    session, store, pk, job, _ = await _read(request, job_id)
    binding = job.get("source_binding")
    if job.get("menu") != "JEYUK" or job.get("provenance") != "SYNTHETIC":
        raise HTTPException(422, "제육볶음 합성 작업에서만 이전 작업을 탐색할 수 있습니다.")
    if not isinstance(binding, dict) or binding.get("source_id") != "synthetic-rest" or \
            binding.get("device_id") != "mockprofile-110-a-001":
        return {"status": "NOT_BOUND", "candidate": None, "immediately_previous_verified": False}
    current_start = _instant(job.get("actual_placed_at"))
    if not current_start:
        return {"status": "CURRENT_NOT_STARTED", "candidate": None, "immediately_previous_verified": False}
    roster = await _roster(store, session.subject)
    if roster is None or job_id not in roster:
        return {"status": "HISTORY_INCOMPLETE", "candidate": None, "immediately_previous_verified": False}
    async def read_roster():
        semaphore = asyncio.Semaphore(8)
        async def read_one(listed_id):
            async with semaphore:
                row = await _store_call(store, store.read_meta, _pk(session.subject, listed_id))
                return listed_id, row
        return dict(await asyncio.gather(*(read_one(listed_id) for listed_id in roster)))
    snapshots = await read_roster()
    if any(not row or row.get("job_id") != listed_id or type(row.get("version")) is not int
           for listed_id, row in snapshots.items()):
        return {"status": "HISTORY_INCOMPLETE", "candidate": None, "immediately_previous_verified": False}
    if snapshots[job_id] != job:
        raise HTTPException(409, "작업이 조회 중 변경되었습니다. 다시 시도해 주세요.")
    eligible, unknown = [], False
    for listed_id, row in snapshots.items():
        if listed_id == job_id:
            continue
        other = row.get("source_binding")
        # A known different menu and profile with no matching binding is unrelated.
        if row.get("menu") not in (None, "JEYUK") and row.get("equipment_profile") not in (None, job.get("equipment_profile")) and \
                (not isinstance(other, dict) or any(other.get(key) != binding.get(key) for key in ("source_id", "device_id"))):
            continue
        started = _instant(row.get("actual_placed_at"))
        if not started:
            if row.get("status") not in {"PENDING", "READY"}:
                unknown = True
            continue
        if started > current_start:
            continue
        if not isinstance(other, dict) or not other.get("source_id") or not other.get("device_id"):
            unknown = True
            continue
        if any(other.get(key) != binding.get(key) for key in ("source_id", "device_id")):
            continue
        if started == current_start:
            unknown = True
        elif started < current_start:
            eligible.append((started, listed_id, row))
    # A second strong read can detect changes, but does not turn a paginated
    # multi-item query into a transactional snapshot or prove legacy listing coverage.
    if await _roster(store, session.subject) != roster:
        raise HTTPException(409, "작업 목록이 조회 중 변경되었습니다. 다시 시도해 주세요.")
    if await read_roster() != snapshots:
        raise HTTPException(409, "작업 이력이 조회 중 변경되었습니다. 다시 시도해 주세요.")
    if unknown:
        status, candidate = "HISTORY_INCOMPLETE", None
    elif not eligible:
        status, candidate = "NO_CANDIDATE", None
    else:
        latest_time = max(entry[0] for entry in eligible)
        latest = [entry for entry in eligible if entry[0] == latest_time]
        if len(latest) != 1:
            status, candidate = "AMBIGUOUS", None
        else:
            started, selected_id, selected = latest[0]
            end = _instant(selected.get("actual_lifted_at"))
            status = "CANDIDATE" if selected.get("menu") == "JEYUK" and \
                     selected.get("provenance") == "SYNTHETIC" and \
                     selected.get("equipment_profile") == job.get("equipment_profile") and \
                     selected.get("status") in {"COMPLETED", "FAILED", "CANCELED"} and \
                     (end is None or started <= end <= current_start) else "UNAVAILABLE"
            candidate = {"job_id": selected_id, "status": selected.get("status"),
                         "safety_status": selected.get("safety_status"), "started_at": started.isoformat()}
    return {"status": status, "candidate": candidate, "listed_jobs_checked": len(roster),
            "immediately_previous_verified": False}
