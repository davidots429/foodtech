"""Dedicated internal delivery; no customer route or credential response."""
import asyncio
import json
import re

import httpx

from foodtech_contracts.recipe_read_handoff import CONTENT_TYPE
from .recipe_read_broker import RecipeReadLease
from .recipe_scope import RecipeScopeDenied


class RecipeDeliveryUnavailable(Exception):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate gateway outcome')
        result[key] = value
    return result


def _outcome(raw, execution_id):
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict) or set(value) != {'execution_id', 'state', 'response_text', 'error_code'} or \
            value['execution_id'] != execution_id or not isinstance(value['state'], str) or \
            value['state'] not in {'SUCCEEDED', 'FAILED', 'DENIED', 'INDETERMINATE'}:
        raise ValueError('invalid gateway outcome')
    if value['state'] == 'SUCCEEDED':
        if not isinstance(value['response_text'], str) or not 1 <= len(value['response_text'].encode('utf-8')) <= 16384 or \
                value['error_code'] is not None:
            raise ValueError('invalid gateway outcome')
    elif value['response_text'] is not None or not isinstance(value['error_code'], str) or \
            not re.fullmatch(r'[A-Z0-9_]{1,128}', value['error_code']):
        raise ValueError('invalid gateway outcome')
    return value


async def deliver_internal_recipe_read_lease(lease):
    """Used only after fresh customer authorization; never retry invocation.

    The invocation route is intentionally not enabled until authoritative
    recommendation evidence and deployed permission checks are connected.
    """
    if type(lease) is not RecipeReadLease:
        raise RecipeDeliveryUnavailable('recipe invocation unavailable')
    try:
        from .ai import _client, _gateway_settings
        handoff = await asyncio.to_thread(lease._internal_handoff)
        raw = handoff._encode_for_internal_tls()
        url, ca, cert, key = _gateway_settings()
        async with _client(ca, cert, key) as client:
            async with client.stream('POST', f'{url}/internal/ai/recipe-executions/{lease.scope.execution_id}/invoke',
                    content=raw, headers={'Content-Type': CONTENT_TYPE, 'Cache-Control': 'no-store',
                                          'Accept': 'application/json'}, follow_redirects=False) as response:
                if response.status_code != 200 or response.headers.get('Cache-Control') != 'no-store':
                    raise ValueError('gateway invocation unavailable')
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 24576:
                        raise ValueError('gateway outcome too large')
                    content.extend(chunk)
        return _outcome(content, lease.scope.execution_id)
    except (ValueError, TypeError, AttributeError, RecipeScopeDenied, httpx.HTTPError, OSError, RecursionError, UnicodeError):
        # No upstream error body, request credentials or URL becomes an error.
        raise RecipeDeliveryUnavailable('recipe invocation unavailable') from None
