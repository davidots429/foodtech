"""Read Gateway-owned execution bindings through the existing private mTLS path."""
import asyncio
import json
from uuid import UUID

import httpx

from foodtech_contracts.recipe_execution_scope import parse_recipe_execution_observation

from .auth import _csrf, _session
from .recipe_scope import RecipeScopeDenied
from .subject_identity import valid_subject


def _unique_members(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError('duplicate scope metadata')
        values[key] = value
    return values


class GatewayRecipeExecutionReader:
    """Internal synchronous reader for authenticated_recipe_scope's worker threads.

    The caller owns the configured mTLS client. Only server-derived subject and
    execution ID are accepted; this class never accepts browser-supplied tenant,
    model, source keys, credentials, redirects or cookie forwarding.
    """
    def __init__(self, *, client, url, subject):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'127.0.0.1', 'localhost'} or \
                parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'} or \
                not valid_subject(subject):
            raise RecipeScopeDenied('recipe execution reader unavailable')
        self.client, self.url, self.subject = client, url.rstrip('/'), subject

    def __call__(self, execution_id):
        try:
            if not isinstance(execution_id, str) or str(UUID(execution_id)) != execution_id:
                raise ValueError
            with self.client.stream('POST', f'{self.url}/internal/ai/recipe-scopes/{execution_id}',
                    json={'tenant_id': self.subject}, follow_redirects=False) as response:
                if response.status_code != 200 or response.headers.get('Cache-Control') != 'no-store':
                    raise ValueError
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 4096:
                        raise ValueError
            payload = json.loads(content, object_pairs_hook=_unique_members)
            return parse_recipe_execution_observation(payload, execution_id=execution_id, tenant_id=self.subject)
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError, OSError, RecursionError):
            # Upstream body and metadata never become a customer error message.
            raise RecipeScopeDenied('recipe execution scope unavailable') from None


async def issue_customer_recipe_reads(request, execution_id, *, job_reader, broker):
    """Connect a fresh customer/CSRF check to Gateway observations and STS.

    No HTTP response is produced. The returned private lease still requires a
    protected Gateway transfer and atomic source/grant settlement before use.
    """
    from .ai import _gateway_settings
    from .recipe_read_broker import issue_authenticated_reads
    import ssl

    session = await _session(request, 'customer')
    _csrf(request, session)
    url, ca, cert, key = _gateway_settings()
    context = ssl.create_default_context(cafile=ca)
    context.load_cert_chain(certfile=cert, keyfile=key)
    # Short metadata reads; never share browser cookies, proxy environment or
    # the Gateway's long-lived IAM credentials with this client.
    client = httpx.Client(verify=context, timeout=20, trust_env=False, follow_redirects=False)
    try:
        reader = GatewayRecipeExecutionReader(client=client, url=url, subject=session.subject)
        return await issue_authenticated_reads(request, execution_id,
            execution_reader=reader, job_reader=job_reader, broker=broker)
    finally:
        await asyncio.to_thread(client.close)
