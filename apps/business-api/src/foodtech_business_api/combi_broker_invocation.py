"""Run a transport-authenticated BFF delegation inside the independent broker."""
import asyncio
import time

from botocore.exceptions import BotoCoreError,ClientError
from .combi_delegation import CombiInvocationDelegation
from .combi_execution_reader import DynamoCombiExecutionReader
from .combi_prior_history import DynamoCombiPriorHistoryAuthority
from .combi_read_broker import CombiReadBroker
from .combi_read_delivery import CombiDeliverySettings,deliver_internal_combi_handoff
from .combi_read_scope import _resolve_observed_combi_scope
from .prior_history_authority import PriorHistoryUnavailable
from .recipe_scope import RecipeScopeDenied


def _scope(delegation,reader,history,clock):
    def live():
        now=clock();delegation.require_live(now);return now
    return _resolve_observed_combi_scope(delegation.subject,delegation.epoch,delegation.execution_id,
        execution_reader=reader,history_authority=history,account_time=live)


async def invoke_delegated_combi(delegation, *, broker, reader, history, gateway, clock=None):
    """Never return an STS lease/frame; only the sanitized Gateway outcome leaves."""
    if type(delegation) is not CombiInvocationDelegation or type(broker) is not CombiReadBroker or \
            type(reader) is not DynamoCombiExecutionReader or type(history) is not DynamoCombiPriorHistoryAuthority or \
            type(gateway) is not CombiDeliverySettings or reader.subject!=delegation.subject or reader.epoch!=delegation.epoch or \
            reader.table!='foodtech-ai-execution-registry-'+broker.stage or \
            history.auth!='foodtech-auth-sessions-'+broker.stage or \
            history.cooking_arn!=f'arn:aws:dynamodb:ap-southeast-2:{broker.account}:table/foodtech-cooking-jobs-{broker.stage}':
        raise RecipeScopeDenied('combi broker scope unavailable')
    if clock is None: clock=time.time
    last=0
    def live_time():
        nonlocal last
        now=clock()
        delegation.require_live(now)
        if now<last: raise ValueError('combi clock moved backwards')
        last=now
        return now
    lease=None
    try:
        scope=await asyncio.to_thread(_scope,delegation,reader,history,live_time)
        pending=asyncio.create_task(asyncio.to_thread(broker.issue,scope,execution_reader=reader,history_authority=history))
        try: lease=await asyncio.shield(pending)
        except BaseException:
            def discard(task):
                if not task.cancelled():
                    try: task.result().close()
                    except Exception: pass
            pending.add_done_callback(discard)
            raise
        if await asyncio.to_thread(_scope,delegation,reader,history,live_time)!=scope:
            raise RecipeScopeDenied('combi scope changed')
        live_time()
        handoff=await asyncio.to_thread(lease._internal_handoff,scope,execution_reader=reader,history_authority=history)
        live_time()
        result=await deliver_internal_combi_handoff(handoff,gateway)
        live_time()
        # The execution is expected to be terminal now; do not mistake that for
        # a change to the original PENDING authorization. Recheck live USER via
        # protected history instead of reissuing or retrying a provider call.
        await asyncio.to_thread(history.revalidate,scope.history)
        live_time()
        return result
    except (ValueError,TypeError,AttributeError,KeyError,ArithmeticError,PriorHistoryUnavailable,BotoCoreError,ClientError):
        raise RecipeScopeDenied('combi broker scope unavailable') from None
    finally:
        if lease is not None: lease.close()
