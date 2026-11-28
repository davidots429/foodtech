"""Dispatch a verified fryer read handoff to the pinned internal Gateway."""
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
import httpx

from foodtech_contracts.tonkatsu_read_handoff import CONTENT_TYPE, TonkatsuReadHandoff
from foodtech_contracts.tonkatsu_read_scope import TonkatsuReadScope

from .combi_read_delivery import CombiDeliverySettings, _deliver_pinned
from .recipe_scope import RecipeScopeDenied


async def deliver_internal_tonkatsu_handoff(handoff, scope, settings):
    try:
        if (type(handoff) is not TonkatsuReadHandoff or type(scope) is not TonkatsuReadScope
            or type(settings) is not CombiDeliverySettings):
            raise ValueError
        handoff.validate(scope)
        credentials = tuple(handoff._sdk_credentials().values())
        path = "/internal/ai/tonkatsu-recipe-executions/" + scope.execution_id + "/invoke"
        return await _deliver_pinned(settings, handoff, credentials, path, CONTENT_TYPE, scope.execution_id)
    except (ValueError, TypeError, AttributeError, OSError, RecursionError, httpx.HTTPError,
            x509.ExtensionNotFound, x509.DuplicateExtension, UnsupportedAlgorithm):
        raise RecipeScopeDenied("fryer invocation unavailable") from None
