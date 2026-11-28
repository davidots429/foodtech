"""An approved proposal becomes a new immutable candidate, never an applied recipe."""
import copy
import re
from .recipe_review_store import validate_review

CANDIDATE_PREFIX = 'RECIPE_CANDIDATE#REVIEW#'


def _lineage(base, equipment, menu):
    if type(base) is not str or not 1 <= len(base) <= 80 or base != base.strip() or \
            type(equipment) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}', equipment) or \
            menu not in {'TONKATSU', 'JEYUK'}:
        raise ValueError('invalid recipe candidate lineage')


def build_candidate(review, job):
    review = validate_review(review)
    if review['decision'] != 'APPROVE' or type(job) is not dict or job.get('job_id') != review['job_id'] or \
            job.get('version') != review['job_version'] or job.get('menu') != review['proposal']['menu'] or \
            job.get('provenance') != 'SYNTHETIC':
        raise ValueError('approved review and matching synthetic job required')
    base, equipment, menu = job.get('recipe_version'), job.get('equipment_profile'), job.get('menu')
    _lineage(base, equipment, menu)
    basis = review["proposal"].get("review_basis")
    if basis is not None and (basis["reference_recipe_version"] != base or basis["profile"] != job.get("thickness_profile")):
        raise ValueError("fryer candidate baseline changed")
    return {'schema_version': 'recipe-candidate-v1', 'candidate_id': 'recipe-candidate-' + review['job_id'] + '-r' + str(review['version']),
            'status': 'CANDIDATE_NOT_APPLIED', 'provenance': 'SYNTHETIC', 'base_recipe_version': base,
            'equipment_profile': equipment, 'review_version': review['version'],
            'review_request_id': review['request_id'], 'execution_id': review['execution_id'],
            'job_id': review['job_id'], 'job_version': review['job_version'],
            'proposal_digest': review['proposal_digest'], 'proposal': copy.deepcopy(review['proposal']),
            'created_at': review['reviewed_at'],
            **({"source_contract": review["source_contract"]} if "source_contract" in review else {})}


def validate_candidate(candidate, review):
    review = validate_review(review)
    if type(candidate) is not dict:
        raise ValueError('invalid recipe candidate')
    _lineage(candidate.get('base_recipe_version'), candidate.get('equipment_profile'), review['proposal']['menu'])
    expected = build_candidate(review, {'job_id': review['job_id'], 'version': review['job_version'],
        'menu': review['proposal']['menu'], 'provenance': 'SYNTHETIC',
        'recipe_version': candidate['base_recipe_version'], 'equipment_profile': candidate['equipment_profile'],
        'thickness_profile': review['proposal'].get('review_basis', {}).get('profile')})
    if candidate != expected:
        raise ValueError('recipe candidate changed or mismatched')
    return copy.deepcopy(expected)
