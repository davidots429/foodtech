"""Separate broker application. No customer session/cookie or credential response API."""
import asyncio
import os
from uuid import UUID
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect
from botocore.exceptions import BotoCoreError,ClientError

from .combi_broker_invocation import invoke_delegated_combi
from .combi_broker_runtime import configured_combi_broker_runtime
from .combi_broker_tls import require_bff_transport
from .combi_delegation import CombiInvocationDelegation,CONTENT_TYPE,MAX_BYTES
from .combi_read_delivery import CombiDeliveryUnavailable
from .recipe_scope import RecipeScopeDenied

app=FastAPI(title='Foodtech private Combi broker',docs_url=None,redoc_url=None,openapi_url=None)


@app.post('/internal/combi/invocations/{execution_id}')
async def invoke(request:Request,execution_id:UUID):
    async def dispatch(delegation, runtime):
        return await invoke_delegated_combi(delegation, broker=runtime.broker, reader=runtime.reader,
            history=runtime.history, gateway=runtime.gateway)

    return await handle_broker_invocation(request, execution_id, recipe="combi",
        delegation_type=CombiInvocationDelegation, content_type=CONTENT_TYPE,
        configure=configured_combi_broker_runtime, dispatch=dispatch)


async def handle_broker_invocation(request, execution_id, *, recipe, delegation_type, content_type, configure, dispatch):
    require_bff_transport(request, recipe=recipe)
    key = recipe.upper()
    if os.getenv(f"FOODTECH_{key}_BROKER_ENABLED") != "true":
        raise HTTPException(503,f'{key}_BROKER_UNAVAILABLE',headers={'Cache-Control':'no-store'})
    raw=bytearray();runtime=None
    try:
        length=request.headers.get('Content-Length')
        if request.headers.get('Content-Type')!=content_type or request.headers.get('Content-Encoding','identity')!='identity' or \
                (length is not None and (len(length)>4 or not length.isascii() or not length.isdigit() or not 0<int(length)<=MAX_BYTES)):
            raise ValueError
        async for chunk in request.stream():
            if len(raw)+len(chunk)>MAX_BYTES: raise ValueError
            raw.extend(chunk)
        if length is not None and len(raw)!=int(length): raise ValueError
        delegation=delegation_type.decode_authenticated_bff(bytes(raw))
        if delegation.execution_id!=str(execution_id): raise ValueError
        pending=asyncio.create_task(asyncio.to_thread(configure,delegation))
        try: runtime=await asyncio.shield(pending)
        except BaseException:
            def discard(task):
                if not task.cancelled():
                    try: task.result().close()
                    except Exception: pass
            pending.add_done_callback(discard)
            raise
        outcome=await dispatch(delegation, runtime)
        return JSONResponse(outcome,headers={'Cache-Control':'no-store'})
    except (ValueError,TypeError,UnicodeError,ClientDisconnect):
        raise HTTPException(400,f'{key}_DELEGATION_UNAVAILABLE',headers={'Cache-Control':'no-store'}) from None
    except RecipeScopeDenied:
        raise HTTPException(409,f'{key}_SCOPE_CHANGED',headers={'Cache-Control':'no-store'}) from None
    except (CombiDeliveryUnavailable,BotoCoreError,ClientError,OSError):
        raise HTTPException(503,f'{key}_INVOCATION_UNCONFIRMED',headers={'Cache-Control':'no-store'}) from None
    finally:
        raw[:]=b'\x00'*len(raw)
        if runtime is not None: await asyncio.to_thread(runtime.close)
