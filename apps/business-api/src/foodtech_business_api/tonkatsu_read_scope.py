"""Translate the shared fryer reader's failures at the business API boundary."""
from botocore.exceptions import BotoCoreError, ClientError

from foodtech_contracts.dynamo_tonkatsu_scope import DynamoTonkatsuReadScope as SharedDynamoTonkatsuReadScope
from foodtech_contracts.dynamo_tonkatsu_scope import TonkatsuScopeUnavailable
from foodtech_contracts.tonkatsu_read_scope import TonkatsuReadScope as TonkatsuReadScope

from .recipe_scope import RecipeScopeDenied


class DynamoTonkatsuReadScope(SharedDynamoTonkatsuReadScope):
    def resolve(self, *, owner, epoch, execution_id):
        try:
            return super().resolve(owner=owner, epoch=epoch, execution_id=execution_id)
        except (TonkatsuScopeUnavailable, BotoCoreError, ClientError):
            raise RecipeScopeDenied("verified fryer read scope unavailable") from None
