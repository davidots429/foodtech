"""Dedicated fryer broker with no browser session or credential response API."""
from uuid import UUID

from fastapi import FastAPI, Request

from .combi_broker_app import handle_broker_invocation
from .combi_broker_runtime import configured_tonkatsu_broker_runtime
from .combi_delegation import TONKATSU_CONTENT_TYPE, TonkatsuInvocationDelegation
from .tonkatsu_broker_invocation import invoke_delegated_tonkatsu


app = FastAPI(title="Foodtech private tonkatsu broker", docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/internal/tonkatsu/invocations/{execution_id}")
async def invoke(request: Request, execution_id: UUID):
    async def dispatch(delegation, runtime):
        return await invoke_delegated_tonkatsu(delegation, broker=runtime.broker, gateway=runtime.gateway)

    return await handle_broker_invocation(request, execution_id, recipe="tonkatsu",
        delegation_type=TonkatsuInvocationDelegation, content_type=TONKATSU_CONTENT_TYPE,
        configure=configured_tonkatsu_broker_runtime, dispatch=dispatch)
