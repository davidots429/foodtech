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
                  and pointer.get("assessment_sk") == assessment.get("sk")
                  and assessment.get("status") == "COMPLETED"
                  and type(pointer.get("quality_passed")) is bool and pointer.get("score_percent") is not None
                  and pointer.get("quality_passed") == assessment.get("quality_passed")
                  and pointer.get("score_percent") == assessment.get("score_percent")
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
    recorded_batch = job.get("tonkatsu_batch")
    fields["tonkatsu_batch"] = ({key: recorded_batch.get(key) for key in
        ("rule_version", "mode", "product_weights_g", "product_count", "total_mass_kg",
         "rated_capacity_kg", "load_percent")} if isinstance(recorded_batch, dict) else None)
    raw_initial = job.get("tonkatsu_initial_context")
    initial_current = bool(isinstance(raw_initial, dict) and
                           raw_initial.get("batch_version") == job.get("tonkatsu_batch_version") and
                           raw_initial.get("schema_version") == "tonkatsu-initial-context-v1" and
                           raw_initial.get("provenance") == "SYNTHETIC" and
                           raw_initial.get("status") == "DECLARED_UNVERIFIED")
    initial = ({key: raw_initial.get(key) for key in ("schema_version", "provenance", "status", "batch_version",
                "product_classification", "ingredient_temperature_c", "temperature_observed_at", "storage_state",
                "oil_capacity_l", "oil_batches_since_change", "oil_cooked_mass_kg", "oil_changed_at")}
               if initial_current else None)
    fields["tonkatsu_initial_context"] = initial
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
        if raw_initial and not initial_current:
            missing.append("INITIAL_CONTEXT_STALE")
        if initial_current:
            missing.append("INITIAL_CONTEXT_DECLARED_UNVERIFIED")
        if not initial or initial.get("ingredient_temperature_c") is None or \
                not initial.get("temperature_observed_at") or initial.get("storage_state") not in {"CHILLED", "FROZEN", "OTHER"}:
            missing.append("INGREDIENT_INITIAL_STATE")
        if initial and initial.get("storage_state") in {"FROZEN", "OTHER"}:
            missing.append("STORAGE_OUTSIDE_SCENARIO")
        if not recorded_batch or not initial or any(initial.get(name) is None for name in
                ("oil_capacity_l", "oil_batches_since_change", "oil_cooked_mass_kg")):
            missing.append("FRYER_AND_BATCH_BASELINE")
        missing.append("COMPARABLE_PREVIOUS_JOB")
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
