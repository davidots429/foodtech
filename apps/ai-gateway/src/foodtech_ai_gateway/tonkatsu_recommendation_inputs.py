"""Derive local-model inputs from two receipt-verified fryer observations."""
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.tonkatsu_controller_samples import TonkatsuControllerSummary
from foodtech_contracts.tonkatsu_observed_inputs import ObservedTonkatsuBatch, TonkatsuBatchFacts, TonkatsuCoreSummary


def build_tonkatsu_inputs(current, prior):
    for batch in (current, prior):
        if (type(batch) is not ObservedTonkatsuBatch or batch.unresolved
            or type(batch.batch_facts) is not TonkatsuBatchFacts or batch.reference_recipe_version is None
            or type(batch.core_summary) is not TonkatsuCoreSummary
            or batch.recorded_safety_status not in {"PASSED", "FAILED"}
            or type(batch.controller_summary) is not TonkatsuControllerSummary
            or batch.controller_summary.calibration_all_ok is not True
            or batch.controller_summary.preplacement_calibration_ok is not True
        ):
            raise ValueError("verified fryer evidence unavailable")
    if current.job_id == prior.job_id or current.profile != prior.profile:
        raise ValueError("comparable fryer jobs required")
    return Evidence("PARTIAL", prior.recorded_safety_status, True, False), prior.as_baseline(), {
        "basis": "AUTHORIZED_SYNTHETIC_FRYER_BATCHES",
        "same_reference_recipe_version": (
            current.reference_recipe_version == prior.reference_recipe_version
        ),
        "current": current.model_context(), "immediately_prior": prior.model_context(),
    }
