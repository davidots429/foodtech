"""Adapt trusted combi observations after independent ownership/history checks.

No HTTP handler accepts these objects. Constructing observations alone never
establishes the immediately prior job or authorizes a provider call. The dedicated
Combi invocation service additionally requires a live received read lease and
atomic history conditions; this adapter alone never authorizes a model call.
"""
from foodtech_contracts.combi_observed_inputs import ObservedCombiBatch
from foodtech_contracts.combi_recommendation import CombiBaseline
from foodtech_contracts.recommendation_guard import Evidence


def build_combi_inputs(current, prior):
    for batch in (current, prior):
        if type(batch) is not ObservedCombiBatch or type(batch.baseline) is not CombiBaseline or \
                type(batch.unresolved) is not tuple or batch.unresolved or type(batch.quality) is not tuple or \
                batch.recorded_safety_status not in {'PASSED', 'FAILED'} or \
                batch.recorded_safety_status != batch.sampled_safety_status or \
                batch.baseline.safety_basis_valid is not True or batch.baseline.process_interrupted is not False:
            raise ValueError('combi recommendation evidence unavailable')
        quality = dict(batch.quality)
        if quality.get('status') != ('CURRENT' if batch.recorded_safety_status == 'PASSED' else 'BLOCKED_BY_SAFETY'):
            raise ValueError('current combi quality evidence unavailable')
    if current.meal_id == prior.meal_id or current.job_id == prior.job_id or \
            current.source_digest == prior.source_digest or \
            (current.baseline.menu, current.baseline.size) != (prior.baseline.menu, prior.baseline.size):
        raise ValueError('distinct comparable combi batches required')
    # COMPLETE refers to the raw span and required baseline inputs, not an
    # attestation of history coverage or every optional ingredient measurement.
    return Evidence('COMPLETE', prior.recorded_safety_status, True, False), prior.baseline, {
        'basis': 'VALIDATED_SYNTHETIC_COMBI_OBSERVATIONS', 'current': current.model_context(),
        'immediately_prior': prior.model_context()}
