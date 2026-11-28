"""Derive local-model inputs from two receipt-verified fryer observations."""
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.tonkatsu_controller_samples import TonkatsuControllerSummary
from foodtech_contracts.tonkatsu_observed_inputs import ObservedTonkatsuBatch


def build_tonkatsu_inputs(current, prior):
    for batch in (current, prior):
        if (type(batch) is not ObservedTonkatsuBatch or batch.unresolved
            or batch.core_series_consistent is not True or batch.recorded_safety_status not in {"PASSED", "FAILED"}
            or type(batch.controller_summary) is not TonkatsuControllerSummary
            or batch.controller_summary.calibration_all_ok is not True
            or batch.controller_summary.preplacement_calibration_ok is not True
        ):
            raise ValueError("verified fryer evidence unavailable")
    if current.job_id == prior.job_id or current.profile != prior.profile:
        raise ValueError("comparable fryer jobs required")
    return Evidence("PARTIAL", prior.recorded_safety_status, True, False), prior.as_baseline(), {
        "basis": "AUTHORIZED_SYNTHETIC_FRYER_BATCHES",
        "current": current.model_context(), "immediately_prior": prior.model_context(),
    }
