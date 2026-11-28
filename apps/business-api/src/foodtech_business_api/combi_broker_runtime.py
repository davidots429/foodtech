"""Explicit independent broker container identity and owned SDK clients."""
from datetime import datetime,timezone
import os
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError,ClientError
from .combi_delegation import CombiInvocationDelegation, TonkatsuInvocationDelegation
from .prior_history_authority import DynamoPriorHistoryAuthority
from .tonkatsu_read_broker import TonkatsuReadBroker
from .tonkatsu_read_scope import DynamoTonkatsuReadScope
from .combi_execution_reader import DynamoCombiExecutionReader
from .combi_prior_history import DynamoCombiPriorHistoryAuthority
from .combi_read_broker import CombiReadBroker
from .combi_read_delivery import CombiDeliverySettings
from .recipe_scope import RecipeScopeDenied


class CombiBrokerRuntime:
    def __init__(self,broker,reader,history,gateway,clients):
        self.broker,self.reader,self.history,self.gateway=broker,reader,history,gateway
        self._clients=clients

    def __repr__(self): return 'CombiBrokerRuntime()'

    def close(self):
        clients,self._clients=self._clients,[]
        seen=set()
        for client in clients:
            if id(client) in seen: continue
            seen.add(id(client))
            try: client.close()
            except (BotoCoreError,ClientError,OSError): pass


def _open_broker_clients(recipe, role_prefix, clients):
    env = os.environ
    stage = env.get("FOODTECH_STAGE", "")
    prefix = "FOODTECH_" + recipe.upper() + "_"
    arn = env.get(prefix + "BROKER_TASK_ROLE_ARN", "")
    role_id = env.get(prefix + "BROKER_TASK_ROLE_ID", "")
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/" + role_prefix + r"([a-z0-9-]{1,32})", arn)
    if not match or match[2] != stage or not re.fullmatch("[A-Z0-9]{16,128}", role_id) or (
        env.get(prefix + "BROKER_ENABLED") != "true" or env.get("AWS_EXECUTION_ENV") != "AWS_ECS_FARGATE"
        or env.get("FOODTECH_PRINCIPAL_PROVISIONING_ENABLED") != "true"
        or env.get("FOODTECH_AI_REGISTRY_TABLE") != "foodtech-ai-execution-registry-" + stage
        or env.get("FOODTECH_AUTH_TABLE") != "foodtech-auth-sessions-" + stage
    ):
        raise ValueError("broker runtime unavailable")
    gateway = CombiDeliverySettings(env[prefix + "GATEWAY_URL"], env[prefix + "GATEWAY_SERVER_CERT"],
        env[prefix + "BROKER_CLIENT_CERT"], env[prefix + "BROKER_CLIENT_KEY"])
    account = match[1]
    session = boto3.Session(region_name="ap-southeast-2")
    credentials = session.get_credentials()
    if credentials is None or credentials.method != "container-role":
        raise ValueError("broker credentials unavailable")
    config = Config(connect_timeout=5, read_timeout=15, proxies={}, ignore_configured_endpoint_urls=True,
                    account_id_endpoint_mode="disabled", retries={"mode": "standard", "total_max_attempts": 2})
    sts_url = "https://sts.ap-southeast-2.amazonaws.com"
    sts = session.client("sts", region_name="ap-southeast-2", config=config, endpoint_url=sts_url)
    clients.append(sts)
    if sts.meta.endpoint_url != sts_url:
        raise ValueError("broker STS endpoint changed")
    identity = sts.get_caller_identity()
    assumed = re.fullmatch(r"arn:aws:sts::" + account + ":assumed-role/" + role_prefix + stage +
        r"/([A-Za-z0-9+=,.@_-]{2,64})", identity.get("Arn", ""))
    if not assumed or identity.get("Account") != account or identity.get("UserId") != role_id + ":" + assumed[1]:
        raise ValueError("broker role unavailable")
    data_url = "https://dynamodb.ap-southeast-2.amazonaws.com"
    client = session.client("dynamodb", region_name="ap-southeast-2", config=config, endpoint_url=data_url)
    clients.append(client)
    if client.meta.endpoint_url != data_url:
        raise ValueError("broker DynamoDB endpoint changed")
    return account, stage, sts, client, gateway


def _close_failed_clients(clients):
    for client in clients:
        try:
            client.close()
        except (BotoCoreError, ClientError, OSError):
            pass


def configured_combi_broker_runtime(delegation):
    clients = []
    try:
        if type(delegation) is not CombiInvocationDelegation:
            raise ValueError
        delegation.require_live(time.time())
        account, stage, sts, client, gateway = _open_broker_clients("combi", "foodtech-recipe-broker-", clients)
        reader = DynamoCombiExecutionReader(table_name="foodtech-ai-execution-registry-" + stage,
            client=client, subject=delegation.subject, epoch=delegation.epoch)
        history = DynamoCombiPriorHistoryAuthority(auth_table="foodtech-auth-sessions-" + stage,
            cooking_table="foodtech-cooking-jobs-" + stage, account_id=account, client=client, clock=time.time)
        broker = CombiReadBroker(account=account, stage=stage,
            read_role_arn=f"arn:aws:iam::{account}:role/foodtech-combi-source-read-{stage}",
            sts=sts, clock=lambda: datetime.now(timezone.utc))
        delegation.require_live(time.time())
        return CombiBrokerRuntime(broker, reader, history, gateway, clients)
    except (ValueError, TypeError, KeyError, AttributeError, BotoCoreError, ClientError, OSError):
        _close_failed_clients(clients)
        raise RecipeScopeDenied("combi broker runtime unavailable") from None


class TonkatsuBrokerRuntime(CombiBrokerRuntime):
    def __repr__(self):
        return "TonkatsuBrokerRuntime()"


def configured_tonkatsu_broker_runtime(delegation):
    clients = []
    try:
        if type(delegation) is not TonkatsuInvocationDelegation:
            raise ValueError
        delegation.require_live(time.time())
        account, stage, sts, client, gateway = _open_broker_clients("tonkatsu", "foodtech-tonkatsu-broker-", clients)

        def clock():
            now = time.time()
            delegation.require_live(now)
            return int(now)

        history = DynamoPriorHistoryAuthority(auth_table="foodtech-auth-sessions-" + stage,
            cooking_table="foodtech-cooking-jobs-" + stage, account_id=account, client=client, clock=clock)
        resolver = DynamoTonkatsuReadScope(execution_table="foodtech-ai-execution-registry-" + stage,
            data_table="foodtech-data-flow-" + stage, history=history)
        broker = TonkatsuReadBroker(account=account, stage=stage,
            read_role_arn=f"arn:aws:iam::{account}:role/foodtech-tonkatsu-source-read-{stage}",
            sts=sts, resolver=resolver)
        return TonkatsuBrokerRuntime(broker, resolver, history, gateway, clients)
    except (ValueError, TypeError, KeyError, AttributeError, BotoCoreError, ClientError, OSError):
        _close_failed_clients(clients)
        raise RecipeScopeDenied("tonkatsu broker runtime unavailable") from None
