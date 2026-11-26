"""Inputs from the guard's complete, authorized synthetic source observations.

This is not a safety approval or an attestation issuer. The caller must first
establish complete source receipts, protected prior history and current access.
No browser/model payload is an input to this adapter.
"""
from foodtech_contracts.jeyuk_observed_inputs import ObservedJeyukBatch
from foodtech_contracts.jeyuk_safety import REQUIRED_SECONDS, SafetyReading
from foodtech_contracts.recommendation_guard import Evidence


def _critical(batch):
    if type(batch) is not ObservedJeyukBatch or batch.unresolved or \
            batch.recorded_safety_status not in {'PASSED', 'FAILED'} or \
            type(batch.sampled_mixture_safety) is not SafetyReading or \
            batch.sampled_calibration_all_ok is not True or \
            batch.sampled_final_feed_aligned is not True or batch.feed_mass_matches_load is not True:
        return False
    # A stored pass cannot replace the required automatic hold. A failed batch
    # may still supply measured inputs for a corrective recommendation.
    return batch.recorded_safety_status != 'PASSED' or (
        batch.sampled_mixture_safety.status == 'PASSED' and
        batch.sampled_mixture_safety.hold_seconds >= REQUIRED_SECONDS)


def build_jeyuk_inputs(current, prior):
    """Only after a bound guard observed both batches and verified their order.

    COMPLETE describes the guard's receipt-matched consecutive raw snapshot;
    it does not certify an ingredient specification or a validated recipe.
    Unknown critical inputs fail closed instead of becoming PARTIAL/defaults.
    The safety-failure constraint is taken from the PRIOR cooking operation.
    """
    if not _critical(current) or not _critical(prior) or current.job_id == prior.job_id or current.profile != prior.profile:
        raise ValueError('recommendation evidence unavailable')
    return (Evidence('COMPLETE', prior.recorded_safety_status, True, False), prior.as_baseline(),
            {'basis': 'AUTHORIZED_SYNTHETIC_BATCHES', 'current': current.model_context(),
             'immediately_prior': prior.model_context()})
