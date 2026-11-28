"""Issue one scoped read lease and forward one authenticated fryer invocation."""
import asyncio
from datetime import datetime, timezone
import time

from botocore.exceptions import BotoCoreError, ClientError

from .combi_delegation import TonkatsuInvocationDelegation
from .combi_read_delivery import CombiDeliverySettings
from .prior_history_authority import PriorHistoryUnavailable
from .recipe_scope import RecipeScopeDenied
from .tonkatsu_read_broker import TonkatsuReadBroker
from .tonkatsu_read_delivery import deliver_internal_tonkatsu_handoff


async def invoke_delegated_tonkatsu(delegation, *, broker, gateway, clock=time.time):
    if (type(delegation) is not TonkatsuInvocationDelegation or type(broker) is not TonkatsuReadBroker
            or type(gateway) is not CombiDeliverySettings):
        raise RecipeScopeDenied("fryer broker inputs unavailable")
    lease = None
    try:
        delegation.require_live(clock())
        deadline = datetime.fromtimestamp(delegation.session_expires, timezone.utc)
        pending = asyncio.create_task(asyncio.to_thread(broker.issue, owner=delegation.subject,
            epoch=delegation.epoch, execution_id=delegation.execution_id, deadline=deadline))
        try:
            scope, lease = await asyncio.shield(pending)
        except asyncio.CancelledError:
            def discard(task):
                if not task.cancelled() and task.exception() is None:
                    task.result()[1].close()

            pending.add_done_callback(discard)
            raise
        delegation.require_live(clock())
        handoff = await asyncio.to_thread(lease._internal_handoff, scope)
        delegation.require_live(clock())
        result = await deliver_internal_tonkatsu_handoff(handoff, scope, gateway)
        delegation.require_live(clock())
        await asyncio.to_thread(broker.resolver.history.revalidate_tonkatsu, scope.history)
        delegation.require_live(clock())
        return result
    except (ValueError, TypeError, AttributeError, KeyError, ArithmeticError,
            PriorHistoryUnavailable, BotoCoreError, ClientError):
        raise RecipeScopeDenied("fryer invocation scope unavailable") from None
    finally:
        if lease is not None:
            lease.close()
