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
    require_bff_transport(request)
    if os.getenv('FOODTECH_COMBI_BROKER_ENABLED')!='true':
        raise HTTPException(503,'COMBI_BROKER_UNAVAILABLE',headers={'Cache-Control':'no-store'})
    raw=bytearray();runtime=None
    try:
        length=request.headers.get('Content-Length')
        if request.headers.get('Content-Type')!=CONTENT_TYPE or request.headers.get('Content-Encoding','identity')!='identity' or \
                (length is not None and (len(length)>4 or not length.isascii() or not length.isdigit() or not 0<int(length)<=MAX_BYTES)):
            raise ValueError
        async for chunk in request.stream():
            if len(raw)+len(chunk)>MAX_BYTES: raise ValueError
            raw.extend(chunk)
        if length is not None and len(raw)!=int(length): raise ValueError
        delegation=CombiInvocationDelegation.decode_authenticated_bff(bytes(raw))
        if delegation.execution_id!=str(execution_id): raise ValueError
        pending=asyncio.create_task(asyncio.to_thread(configured_combi_broker_runtime,delegation))
        try: runtime=await asyncio.shield(pending)
        except BaseException:
            def discard(task):
                if not task.cancelled():
                    try: task.result().close()
                    except Exception: pass
            pending.add_done_callback(discard)
            raise
        outcome=await invoke_delegated_combi(delegation,broker=runtime.broker,reader=runtime.reader,
            history=runtime.history,gateway=runtime.gateway)
        return JSONResponse(outcome,headers={'Cache-Control':'no-store'})
    except (ValueError,TypeError,UnicodeError,ClientDisconnect):
        raise HTTPException(400,'COMBI_DELEGATION_UNAVAILABLE',headers={'Cache-Control':'no-store'}) from None
    except RecipeScopeDenied:
        raise HTTPException(409,'COMBI_SCOPE_CHANGED',headers={'Cache-Control':'no-store'}) from None
    except (CombiDeliveryUnavailable,BotoCoreError,ClientError,OSError):
        raise HTTPException(503,'COMBI_INVOCATION_UNCONFIRMED',headers={'Cache-Control':'no-store'}) from None
    finally:
        raw[:]=b'\x00'*len(raw)
        if runtime is not None: await asyncio.to_thread(runtime.close)
