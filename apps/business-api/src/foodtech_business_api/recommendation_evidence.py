"""Read-only, allowlisted cooking evidence for a future recommendation request.

The result describes recorded facts, not evidence completeness or authorization
to call a model. Free-text notes, ownership IDs and raw probe values stay out.
"""
from .cooking import _read, _store_call


def _snapshot(job: dict, pointer: dict | None, assessment: dict | None, derived: dict | None) -> dict:
    usable = bool(pointer and pointer.get("usable") is True and not job.get("evaluation_blocked", True)
                  and job.get("safety_status") == "PASSED"
                  and pointer.get("safety_version") == job.get("safety_version")
                  and assessment and assessment.get("version") == pointer.get("version")
                  and assessment.get("safety_version") == job.get("safety_version"))
    process = derived if derived else job
    process_detail = process.get("process_assessment") or {}
    stale = bool(derived and derived.get("job_version_at_derivation") != job.get("version"))
    raw_coverage = derived.get("observation_coverage") if derived else None
    coverage = ({key: raw_coverage.get(key) for key in ("version", "first_sample_at", "last_sample_at",
                "sample_count", "before_first_us", "after_last_us", "internal_gap_detected")}
                if isinstance(raw_coverage, dict) else None)
    quality = ({"version": assessment.get("version"), "scale_version": assessment.get("scale_version"),
                "quality_passed": assessment.get("quality_passed"), "score_percent": assessment.get("score_percent"),
                "actual_mass_kg": assessment.get("actual_mass_kg"),
                "actual_portions": assessment.get("actual_portions"), "load_band": assessment.get("load_band")}
               if usable else None)
    fields = {"provenance": job.get("provenance"), "menu": job.get("menu"),
              "recipe_version": job.get("recipe_version"), "equipment_profile": job.get("equipment_profile"),
              "thickness_cm": job.get("thickness_cm"), "thickness_profile": job.get("thickness_profile"),
              "actual_load_kg": job.get("actual_load_kg"), "cooking_seconds": job.get("actual_cooking_seconds"),
              "job_status": job.get("status"), "safety_status": job.get("safety_status"),
              "process_status": process.get("process_assessment_status"),
              "process_rule_version": process_detail.get("rule_version"), "quality": quality}
    missing = []
    if job.get("provenance") != "SYNTHETIC" or job.get("menu") not in {"TONKATSU", "JEYUK"}:
        missing.append("SUPPORTED_SYNTHETIC_JOB")
    if job.get("status") not in {"COMPLETED", "FAILED"} or not job.get("actual_cooking_seconds"):
        missing.append("COMPLETED_COOKING_TIME")
    if job.get("safety_status") not in {"PASSED", "FAILED"}:
        missing.append("SAFETY_ASSESSMENT")
    if not usable:
        missing.append("CURRENT_QUALITY_ASSESSMENT")
    if not derived or not coverage:
        missing.append("OBSERVATION_COVERAGE")
    if coverage and coverage.get("internal_gap_detected") is True:
        missing.append("DETECTED_PROCESS_GAP")
    # Observed endpoints and absence of a detected internal gap do not prove
    # that the entire source stream or all required features are complete.
    missing.append("SOURCE_COMPLETENESS")
    if stale:
        missing.append("CURRENT_PROCESS_DERIVATION")
    if job.get("menu") == "TONKATSU":
        if not job.get("thickness_profile"):
            missing.append("THICKNESS_PROFILE")
        # These approved input categories do not yet have a complete, trusted
        # per-job contract; do not invent values from the current process state.
        missing.extend(["INGREDIENT_INITIAL_STATE", "FRYER_AND_BATCH_BASELINE", "COMPARABLE_PREVIOUS_JOB"])
    elif job.get("menu") == "JEYUK":
        missing.extend(["TARGET_SETTINGS_AND_INTERVALS", "PREVIOUS_JAM_OR_OVERLOAD", "COMPARABLE_PREVIOUS_JOB"])
    return {"job_id": job["job_id"], "job_version": job["version"],
            "safety_version": job.get("safety_version"),
            "process_view": "LATEST_RESTATED" if derived else "AS_PROCESSED",
            "process_result_version": derived.get("sk") if derived else None,
            "process_stale": stale, "observation_coverage": coverage,
            "recorded": fields, "unverified_categories": missing,
            "completeness": "NOT_ASSESSED", "recommendation_execution_available": False}


async def read_evidence(request, job_id: str) -> dict:
    from fastapi import HTTPException
    _, store, pk, job, pointer = await _read(request, job_id)
    first_process_pointer = await _store_call(store, store.process_pointer, pk)
    derived = await _store_call(store, store.latest_process_result, pk)
    assessment = None
    if pointer and type(pointer.get("version")) is int and pointer["version"] > 0:
        assessment = await _store_call(store, store.assessment, pk, pointer["version"])
    current_job, current_pointer = await _store_call(store, store.read, pk)
    last_process_pointer = await _store_call(store, store.process_pointer, pk)
    if not current_job or current_job.get("version") != job.get("version") or current_pointer != pointer or \
            first_process_pointer != last_process_pointer or \
            (first_process_pointer and (not derived or derived.get("sk") != first_process_pointer.get("result_sk"))):
        raise HTTPException(409, "조리 근거가 변경되었습니다. 다시 조회해 주세요.")
    return _snapshot(job, pointer, assessment, derived)
