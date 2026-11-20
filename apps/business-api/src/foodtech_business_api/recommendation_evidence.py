"""Read-only, allowlisted cooking evidence for a future recommendation request.

The result describes recorded facts, not evidence completeness or authorization
to call a model. Free-text notes, ownership IDs and raw probe values stay out.
"""
import os
from datetime import datetime
from uuid import UUID

from foodtech_contracts import stream_binding_key
from .cooking import _pk, _read, _store_call
from .recommendation_completeness import preliminary
from .source_revision import read_source_revision


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
    receipt = derived.get("collection_evidence") if derived else None
    receipt_match = bool(isinstance(receipt, dict) and receipt.get("raw_snapshot_matches") is True and
                         isinstance(receipt.get("receipt_sk"), str) and not stale)
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
    summary = derived.get("temperature_sample_summary") if derived else None
    fields["temperature_sample_summary"] = ({key: summary.get(key) for key in
        ("version", "basis", "scope", "sample_count", "warning_sample_count", "first_sample_celsius",
         "minimum_sample_celsius", "maximum_sample_celsius", "last_sample_celsius",
         "below_160_between_qualifying_samples_us", "above_180_between_qualifying_samples_us",
         "longest_above_180_sample_span_us")}
         if isinstance(summary, dict) and not stale else None)
    if fields["temperature_sample_summary"] is not None:
        recovery = summary.get("last_sampled_recovery")
        fields["temperature_sample_summary"]["last_sampled_recovery"] = ({key: recovery.get(key) for key in
            ("first_below_at", "first_in_range_at", "span_us", "onset_bracketed_by_samples")}
            if isinstance(recovery, dict) else None)
    preload = derived.get("preplacement_temperature_observation") if derived else None
    fields["preplacement_temperature_observation"] = ({key: preload.get(key) for key in
        ("version", "status", "sample_at", "sample_celsius", "quality_status", "age_before_placement_us")}
        if isinstance(preload, dict) and not stale else None)
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
        if not preload or stale or preload.get("status") != "OBSERVED":
            missing.append("PREPLACEMENT_TEMPERATURE_UNAVAILABLE")
        else:
            missing.append("PREPLACEMENT_RECENCY_NOT_ASSESSED")
            if preload.get("quality_status") == "WARNING":
                missing.append("PREPLACEMENT_TEMPERATURE_FLAGGED")
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
    completeness = preliminary(job, initial_context=initial, coverage=coverage)
    return {"job_id": job["job_id"], "job_version": job["version"],
            "safety_version": job.get("safety_version"),
            "process_view": "LATEST_RESTATED" if derived else "AS_PROCESSED",
            "process_result_version": derived.get("sk") if derived else None,
            "process_stale": stale, "observation_coverage": coverage,
            "collection_snapshot_matches_receipt": receipt_match,
            "recorded": fields, "unverified_categories": missing,
            "completeness": completeness["status"], "completeness_reasons": completeness["reasons"],
            "completeness_rule_version": completeness["rule_version"], "recommendation_execution_available": False}


def _comparison_usable(job: dict | None, pointer: dict | None, record: dict, role: str) -> bool:
    return bool(job and pointer and job.get("status") == "COMPLETED" and job.get("safety_status") == "PASSED" and
                not job.get("evaluation_blocked", True) and pointer.get("usable") is True and
                type(job.get("version")) is int and job["version"] > 0 and
                type(job.get("safety_version")) is int and job["safety_version"] > 0 and
                type(pointer.get("version")) is int and pointer["version"] > 0 and
                pointer.get("safety_version") == job.get("safety_version") == record.get(f"{role}_safety_version") and
                pointer.get("version") == record.get(f"{role}_assessment_version") and
                job.get("version") == record.get(f"{role}_job_version"))


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


async def read_evidence(request, job_id: str, comparison_id: str | None = None) -> dict:
    from fastapi import HTTPException
    session, store, pk, job, pointer = await _read(request, job_id)
    first_process_pointer = await _store_call(store, store.process_pointer, pk)
    derived = await _store_call(store, store.latest_process_result, pk)
    assessment = None
    if pointer and type(pointer.get("version")) is int and pointer["version"] > 0:
        assessment = await _store_call(store, store.assessment, pk, pointer["version"])
    comparison_result = None
    comparison_check = None
    if comparison_id is not None:
        if len(comparison_id) != 64 or any(char not in "0123456789abcdef" for char in comparison_id):
            raise HTTPException(400, "비교 기록 ID가 올바르지 않습니다.")
        selected = await _store_call(store, store.comparison, pk, comparison_id)
        if not selected or selected.get("comparison_id") != comparison_id or selected.get("candidate_job_id") != job_id:
            raise HTTPException(404, "비교 기록을 찾을 수 없습니다.")
        try:
            baseline_id = str(UUID(selected["baseline_job_id"]))
            if baseline_id != selected["baseline_job_id"] or baseline_id == job_id:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise HTTPException(409, "비교 기록의 연결이 올바르지 않습니다.") from None
        baseline_pk = _pk(session.subject, baseline_id)
        baseline, baseline_pointer = await _store_call(store, store.read, baseline_pk)
        selected_pointer = await _store_call(store, store.current_comparison, pk, baseline_id)
        if not selected_pointer or selected_pointer.get("comparison_id") != comparison_id or \
                not _comparison_usable(job, pointer, selected, "candidate") or \
                not _comparison_usable(baseline, baseline_pointer, selected, "baseline") or \
                selected.get("provenance") != "SYNTHETIC" or job.get("provenance") != "SYNTHETIC" or \
                not baseline or baseline.get("provenance") != "SYNTHETIC" or \
                selected.get("menu") != job.get("menu") or baseline.get("menu") != job.get("menu") or \
                selected.get("equipment_profile") != job.get("equipment_profile") or \
                baseline.get("equipment_profile") != job.get("equipment_profile"):
            raise HTTPException(409, "선택한 비교의 평가 또는 조리작업이 변경되었습니다.")
        baseline_end, candidate_end = _instant(baseline.get("actual_lifted_at")), _instant(job.get("actual_lifted_at"))
        try:
            earlier = bool(baseline_end and candidate_end and baseline_end < candidate_end)
        except (ValueError, OverflowError):
            earlier = False
        if not earlier:
            raise HTTPException(409, "선택한 비교가 현재 작업보다 이전인지 확인할 수 없습니다.")
        comparison_result = {key: selected.get(key) for key in ("comparison_id", "baseline_job_id",
            "baseline_recipe_version", "baseline_actual_mass_kg", "baseline_actual_portions", "baseline_load_band",
            "baseline_score_percent", "baseline_quality_passed", "candidate_score_percent", "score_total_delta")}
        comparison_result.update({"status": "SELECTED_COMPARABLE", "baseline_lifted_at": baseline_end.isoformat(),
                                  "immediately_previous_verified": False})
        comparison_check = (baseline_pk, baseline, baseline_pointer, baseline_id, selected_pointer)
    source_status = "NOT_TRACKED"
    source_pk = None
    first_revision = None
    derived_revision = derived.get("source_revision") if derived else None
    table = os.getenv("FOODTECH_DATA_TABLE", "")
    if type(derived_revision) is int and derived_revision > 0:
        binding = job.get("source_binding")
        try:
            expected = stream_binding_key(**binding) if isinstance(binding, dict) else None
        except (TypeError, ValueError, KeyError):
            expected = None
        if not expected or derived.get("source_stream_pk") != expected:
            source_status = "INCONSISTENT"
        elif not table:
            source_status = "NOT_CONFIGURED"
        else:
            source_pk = expected
            try:
                first_revision = await read_source_revision(table, source_pk)
            except Exception as exc:
                raise HTTPException(503, "원천 버전을 조회하지 못했습니다.") from exc
    current_job, current_pointer = await _store_call(store, store.read, pk)
    last_process_pointer = await _store_call(store, store.process_pointer, pk)
    if comparison_check:
        baseline_pk, baseline, baseline_pointer, baseline_id, selected_pointer = comparison_check
        latest_baseline, latest_baseline_pointer = await _store_call(store, store.read, baseline_pk)
        latest_selected_pointer = await _store_call(store, store.current_comparison, pk, baseline_id)
        if latest_baseline != baseline or latest_baseline_pointer != baseline_pointer or \
                latest_selected_pointer != selected_pointer:
            raise HTTPException(409, "선택한 비교가 조회 중 변경되었습니다. 다시 조회해 주세요.")
    if source_pk:
        try:
            last_revision = await read_source_revision(table, source_pk)
        except Exception as exc:
            raise HTTPException(503, "원천 버전을 조회하지 못했습니다.") from exc
        if first_revision != last_revision:
            raise HTTPException(409, "원천 자료가 조회 중 변경되었습니다. 다시 조회해 주세요.")
        source_status = ("NOT_TRACKED" if last_revision is None else
                         "MATCHED_AT_READ" if last_revision == derived_revision else "CHANGED")
    if not current_job or current_job.get("version") != job.get("version") or current_pointer != pointer or \
            first_process_pointer != last_process_pointer or \
            (first_process_pointer and (not derived or derived.get("sk") != first_process_pointer.get("result_sk"))):
        raise HTTPException(409, "조리 근거가 변경되었습니다. 다시 조회해 주세요.")
    return {**_snapshot(job, pointer, assessment, derived), "source_revision_status": source_status,
            "selected_comparison": comparison_result}
