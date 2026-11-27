"""BFF session/CSRF boundary -> separate broker. Never issue/read STS here."""
import os
import time

from botocore.exceptions import BotoCoreError,ClientError
from fastapi import HTTPException
from fastapi.responses import JSONResponse
import httpx
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm

from .auth import _csrf,_session
from .combi_delegation import CombiInvocationDelegation,CONTENT_TYPE
from .combi_read_delivery import CombiDeliverySettings,_context
from .recipe_read_delivery import _outcome
from .recipe_scope import _require_principal_record


class CombiBrokerDeliveryUnavailable(Exception): pass


def configured_bff_broker_settings():
    return CombiDeliverySettings(os.environ['FOODTECH_COMBI_BROKER_URL'],os.environ['FOODTECH_COMBI_BROKER_SERVER_CERT'],
        os.environ['FOODTECH_COMBI_BFF_CERT'],os.environ['FOODTECH_COMBI_BFF_KEY'])


async def deliver_bff_delegation(delegation,settings):
    try:
        if type(delegation) is not CombiInvocationDelegation or type(settings) is not CombiDeliverySettings: raise ValueError
        delegation.require_live(time.time())
        context=_context(settings)
        async with httpx.AsyncClient(verify=context,timeout=195,trust_env=False,follow_redirects=False,http2=False) as client:
            async with client.stream('POST',settings.gateway_url+'/internal/combi/invocations/'+delegation.execution_id,
                    content=delegation.encode_for_broker(),headers={'Content-Type':CONTENT_TYPE,'Cache-Control':'no-store',
                        'Accept':'application/json','Accept-Encoding':'identity'}) as response:
                if response.status_code!=200 or response.headers.get('Cache-Control')!='no-store' or \
                        response.headers.get('Content-Type','').split(';',1)[0].lower()!='application/json' or \
                        response.headers.get('Content-Encoding','identity')!='identity': raise ValueError
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw)+len(chunk)>24576: raise ValueError
                    raw.extend(chunk)
        delegation.require_live(time.time())
        return _outcome(raw,delegation.execution_id)
    except (ValueError,TypeError,AttributeError,OSError,httpx.HTTPError,UnicodeError,RecursionError,
            x509.ExtensionNotFound,x509.DuplicateExtension,UnsupportedAlgorithm):
        raise CombiBrokerDeliveryUnavailable('combi invocation unconfirmed') from None


async def invoke_customer_combi(request,execution_id):
    before=await _session(request,'customer');_csrf(request,before)
    await _require_principal_record(before)
    if os.getenv('FOODTECH_COMBI_CUSTOMER_INVOCATION_ENABLED')!='true' or \
            os.getenv('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED')!='true':
        raise HTTPException(503,'콤비 권고 실행 준비가 완료되지 않았습니다.',headers={'Cache-Control':'no-store'})
    async for chunk in request.stream():
        if chunk: raise HTTPException(400,'실행 ID만 지정해 주세요.',headers={'Cache-Control':'no-store'})
    try:
        if before.surface!='customer': raise ValueError
        delegation=CombiInvocationDelegation(before.subject,before.epoch,str(execution_id),before.expires)
        csrf=before.csrf
        delegation.require_live(time.time())
        result=await deliver_bff_delegation(delegation,configured_bff_broker_settings())
        after=await _session(request,'customer');_csrf(request,after)
        await _require_principal_record(after)
        if (after.surface,after.subject,after.epoch,after.csrf)!=('customer',delegation.subject,delegation.epoch,csrf) or \
                type(after.expires) is not int or not time.time()<after.expires:
            raise HTTPException(401,'로그인 상태가 변경되었습니다. 다시 로그인해 주세요.',headers={'Cache-Control':'no-store'})
        return JSONResponse(result,headers={'Cache-Control':'no-store'})
    except (CombiBrokerDeliveryUnavailable,ValueError,TypeError,KeyError,BotoCoreError,ClientError,OSError):
        raise HTTPException(503,'실행 결과를 확인하지 못했습니다. 실행 상태를 조회해 주세요.',headers={'Cache-Control':'no-store'}) from None
