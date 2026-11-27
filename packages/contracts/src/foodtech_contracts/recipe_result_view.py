"""Project a stored recipe proposal for review; never expose raw model output.

This checks the persisted wire schema, not current cooking safety or adoption.
Historical proposals remain proposals; it does not execute or approve a model.
"""
from decimal import Decimal, InvalidOperation
import json

from .recipe_execution_scope import recipe_execution_status_payload, validate_recipe_status_payload
from .recommendation_guard import RULE_VERSION
from .recommendation_output import MAX_OUTPUT_BYTES, SCHEMA_VERSION

_TONKATSU = {'temperature_c', 'duration_seconds'}
_JEYUK = _TONKATSU | {'rpm', 'load_kg', 'vegetable_interval_seconds', 'sauce_interval_seconds'}
_VIEW = {'projection_status', 'proposal', 'error_code'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate recipe result member')
        result[key] = value
    return result


def _data(value):
    if not isinstance(value, dict) or set(value) not in (_TONKATSU, _JEYUK):
        raise ValueError('recipe proposal fields unavailable')
    for key, number in value.items():
        if key in {'duration_seconds', 'vegetable_interval_seconds', 'sauce_interval_seconds'}:
            if type(number) is not int or not 0 <= number < 2**31:
                raise ValueError('invalid stored recipe number')
        else:
            if type(number) is not str or not 1 <= len(number) <= 64 or not number.isascii():
                raise ValueError('invalid stored recipe number')
            try:
                parsed = Decimal(number)
                if not parsed.is_finite() or parsed < 0 or abs(parsed.as_tuple().exponent) > 100 or str(parsed) != number:
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValueError('invalid stored recipe number') from None
    return ('TONKATSU' if set(value) == _TONKATSU else 'JEYUK'), dict(value)


def _proposal(raw):
    if type(raw) is not str or len(raw.encode('utf-8')) > MAX_OUTPUT_BYTES:
        raise ValueError('recipe result unavailable')
    value = json.loads(raw, object_pairs_hook=_unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    if not isinstance(value, dict) or set(value) != {
            'status', 'error_code', 'data', 'schema_version', 'guard_version', 'fallback_used', 'attempts'} or \
            value['status'] != 'NEEDS_REVIEW' or value['error_code'] is not None or \
            value['schema_version'] != SCHEMA_VERSION or value['guard_version'] != RULE_VERSION or \
            value['fallback_used'] is not False or type(value['attempts']) is not int or not 1 <= value['attempts'] <= 3:
        raise ValueError('recipe result unavailable')
    menu, data = _data(value['data'])
    return {'status': 'NEEDS_REVIEW', 'schema_version': SCHEMA_VERSION, 'guard_version': RULE_VERSION,
            'menu': menu, 'data': data}


def validate_recipe_result_payload(payload, *, execution_id, job_id):
    if not isinstance(payload, dict) or not _VIEW <= set(payload):
        raise ValueError('recipe result unavailable')
    base = validate_recipe_status_payload({k: v for k, v in payload.items() if k not in _VIEW},
                                         execution_id=execution_id, job_id=job_id)
    status, proposal, error = (payload[k] for k in ('projection_status', 'proposal', 'error_code'))
    expected = {'NOT_READY': ('PENDING', 'RESERVED'), 'FAILED': ('FAILED',),
                'AVAILABLE': ('SUCCEEDED',), 'UNAVAILABLE': ('SUCCEEDED',)}
    if not isinstance(status, str) or status not in expected or base['state'] not in expected[status]:
        raise ValueError('recipe result unavailable')
    if status == 'AVAILABLE':
        if not isinstance(proposal, dict) or set(proposal) != {'status', 'schema_version', 'guard_version', 'menu', 'data'} or \
                proposal['status'] != 'NEEDS_REVIEW' or proposal['schema_version'] != SCHEMA_VERSION or \
                proposal['guard_version'] != RULE_VERSION or error is not None:
            raise ValueError('recipe proposal unavailable')
        menu, data = _data(proposal['data'])
        if proposal['menu'] != menu:
            raise ValueError('recipe proposal unavailable')
        proposal = {**proposal, 'data': data}
    elif proposal is not None or error != {'NOT_READY': None, 'FAILED': 'RECIPE_EXECUTION_FAILED',
                                          'UNAVAILABLE': 'RECIPE_RESULT_UNAVAILABLE'}[status]:
        raise ValueError('recipe result unavailable')
    return {**base, 'projection_status': status, 'proposal': proposal, 'error_code': error}


def recipe_execution_result_payload(snapshot, result, *, execution_id, tenant_id, job_id):
    base = recipe_execution_status_payload(snapshot, result, execution_id=execution_id, tenant_id=tenant_id, job_id=job_id)
    state = base['state']
    proposal, error = None, None
    if state in {'PENDING', 'RESERVED'}:
        status = 'NOT_READY'
    elif state == 'FAILED':
        status, error = 'FAILED', 'RECIPE_EXECUTION_FAILED'
    else:
        try:
            proposal = _proposal(result.get('response_text'))
            status = 'AVAILABLE'
        except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
            status, error = 'UNAVAILABLE', 'RECIPE_RESULT_UNAVAILABLE'
    return validate_recipe_result_payload({**base, 'projection_status': status, 'proposal': proposal, 'error_code': error},
                                          execution_id=execution_id, job_id=job_id)
