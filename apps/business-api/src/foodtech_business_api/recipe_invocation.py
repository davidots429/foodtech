"""Customer invocation using only server-observed execution, history and source scope."""
import asyncio
import os
import time

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .auth import _csrf, _session
from .cooking_store import DynamoCookingStore
from .gateway_recipe_scope import issue_customer_recipe_reads
from .prior_history_authority import DynamoPriorHistoryAuthority, PriorHistoryUnavailable
from .recipe_read_broker import configured_broker
from .recipe_read_delivery import RecipeDeliveryUnavailable, deliver_internal_recipe_read_lease
from .recipe_scope import RecipeScopeDenied, _require_principal_record
from .source_read_authority import DynamoSourceReadAuthority


def _store_and_history(broker):
    authority = broker.source_authority
    if type(authority) is not DynamoSourceReadAuthority or broker.cooking_resource is None or \
            authority.auth_table != f'foodtech-auth-sessions-{broker.stage}' or \
            authority.catalog_table != f'foodtech-source-access-{broker.stage}':
        raise RecipeScopeDenied('recipe runtime unavailable')
    cooking_table = f'foodtech-cooking-jobs-{broker.stage}'
    store = DynamoCookingStore(cooking_table, authority.auth_table,
                               dynamodb_resource=broker.cooking_resource, dynamodb_client=authority.client)
    history = DynamoPriorHistoryAuthority(auth_table=authority.auth_table, cooking_table=cooking_table,
                account_id=broker.account, client=authority.client, clock=lambda: int(time.time()))
    return store, history


async def invoke_customer_recipe(request, execution_id):
    session = await _session(request, 'customer')
    _csrf(request, session)
    if os.getenv('FOODTECH_RECIPE_INVOCATION_ENABLED') != 'true' or \
            os.getenv('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') != 'true':
        raise HTTPException(503, '권고 실행 준비가 완료되지 않았습니다.', headers={'Cache-Control': 'no-store'})
    # This route accepts only the path identifier. Never accept browser-supplied
    # tenant IDs, grants, history witnesses, source records or AWS credentials.
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(400, '실행 ID만 지정해 주세요.', headers={'Cache-Control': 'no-store'})
    broker = None
    try:
        await _require_principal_record(session)
        broker = await asyncio.to_thread(configured_broker)
        store, history = _store_and_history(broker)
        lease = await issue_customer_recipe_reads(request, str(execution_id), job_reader=store.read_meta,
                                                  broker=broker, history_authority=history)
        result = await deliver_internal_recipe_read_lease(lease)
        after = await _session(request, 'customer')
        await _require_principal_record(after)
        if (session.subject, session.epoch, session.csrf) != (after.subject, after.epoch, after.csrf):
            raise HTTPException(401, '로그인 상태가 변경되었습니다. 다시 로그인해 주세요.',
                                headers={'Cache-Control': 'no-store'})
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})
    except (RecipeScopeDenied, PriorHistoryUnavailable):
        raise HTTPException(409, '권고 실행의 권한 또는 근거가 변경되었습니다. 다시 조회해 주세요.',
                            headers={'Cache-Control': 'no-store'}) from None
    except (RecipeDeliveryUnavailable, BotoCoreError, ClientError, ValueError, OSError):
        raise HTTPException(503, '권고 실행을 확인하지 못했습니다. 실행 상태를 조회해 주세요.',
                            headers={'Cache-Control': 'no-store'}) from None
    finally:
        if broker is not None:
            clients = (getattr(broker, 'sts', None), getattr(getattr(broker, 'source_authority', None), 'client', None))
            for client in clients:
                close = getattr(client, 'close', None)
                if callable(close):
                    try:
                        await asyncio.to_thread(close)
                    except (BotoCoreError, OSError):
                        pass
