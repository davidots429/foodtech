"""Inspect or explicitly initialize reviewed development Tonkatsu TLS Secrets."""
import json
from dataclasses import dataclass
from pathlib import Path
import re
import signal
import sys
from uuid import UUID

from private_rest_initialize import (
    ACCOUNT, REGION, Arguments, InitializationDeadline, VerifiedOperator, canonical_hash, deadline,
    expected_policy, read_value, verify_operator_environment, verify_store, write_value,
)
from tonkatsu_trial_tls_material import generate, verify_bundles, verify_hostnames

BROKER_STACK = "FoodtechTonkatsuBrokerTls-dev"
PEER_STACK = "FoodtechTonkatsuPeerTls-dev"
INFERENCE_STACK = "FoodtechTonkatsuInferenceTls-dev"
DESTINATIONS = (
    ("business-api", PEER_STACK, "BusinessApi"), ("broker", BROKER_STACK, "Broker"),
    ("gateway", PEER_STACK, "Gateway"),
)
_COMPLETE = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
_KEY_ID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"


@dataclass(frozen=True)
class Destination:
    role: str
    stack: str
    prefix: str
    arn: str
    key: str

    @property
    def name(self):
        return f"foodtech/tonkatsu-{self.role}-dev/tls"


def reviewed_plan(document):
    required = {"request_id", "broker_host", "gateway_host", "stacks", "stores"}
    if type(document) is not dict or set(document) not in (
        required, required | {"records_host"}, required | {"records_host", "inference_host"},
    ):
        raise ValueError("unexpected reviewed plan fields")
    token = UUID(document["request_id"])
    if token.version != 4 or str(token) != document["request_id"]:
        raise ValueError("canonical reviewed request UUID required")
    optional_hosts = ("records_host", "inference_host")
    if any(host in document and document[host] is None for host in optional_hosts):
        raise ValueError("reviewed optional hostnames must be explicit")
    verify_hostnames(document["broker_host"], document["gateway_host"], *(document.get(host) for host in optional_hosts))
    expected = DESTINATIONS
    if "inference_host" in document:
        expected += (("inference", INFERENCE_STACK, "Inference"),)
    hashes = document["stacks"]
    if type(hashes) is not dict or set(hashes) != {entry[1] for entry in expected} or any(
        not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes.values()
    ):
        raise ValueError("reviewed template hashes for every destination required")
    stores = document["stores"]
    if type(stores) is not dict or set(stores) != {entry[0] for entry in expected}:
        raise ValueError("reviewed TLS stores for every destination required")
    destinations = []
    for role, stack, prefix in expected:
        row = stores[role]
        if type(row) is not dict or set(row) != {"arn", "key"}:
            raise ValueError("TLS Secret and key references required")
        target = Destination(role, stack, prefix, row["arn"], row["key"])
        if not re.fullmatch(fr"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{target.name}-[A-Za-z0-9]{{6}}", target.arn) or (
                not re.fullmatch(fr"arn:aws:kms:{REGION}:{ACCOUNT}:key/{_KEY_ID}", target.key)):
            raise ValueError("unexpected TLS storage references")
        destinations.append(target)
    if len({target.key for target in destinations}) != len(destinations):
        raise ValueError("dedicated TLS keys required")
    return destinations


def _expected_policy(target):
    policy = expected_policy(target)
    condition = policy["Statement"][1]["Condition"]["StringLike"]
    context = condition["kms:EncryptionContext:SecretARN"]
    condition["kms:EncryptionContext:SecretARN"] = context.removesuffix("*") + "??????"
    return policy


def _verify_stack(cf, stack_name, expected_hash, destinations):
    (stack,) = cf.describe_stacks(StackName=stack_name)["Stacks"]
    if stack["StackName"] != stack_name or stack["StackStatus"] not in _COMPLETE or stack.get("EnableTerminationProtection") is not True or (
            not re.fullmatch(fr"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{stack_name}/{_KEY_ID}", stack["StackId"])):
        raise ValueError("protected reviewed TLS stack required")
    tags = {item["Key"]: item["Value"] for item in stack["Tags"]}
    if (tags.get("Project"), tags.get("Environment")) != ("foodtech", "dev"):
        raise ValueError("unexpected TLS stack tags")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    expected_outputs = {target.prefix + suffix: value for target in destinations
                        for suffix, value in (("TlsBundleArn", target.arn), ("TlsKeyArn", target.key))}
    if outputs != expected_outputs:
        raise ValueError("TLS stack outputs differ from plan")
    template = cf.get_template(StackName=stack_name, TemplateStage="Original")["TemplateBody"]
    if canonical_hash(json.loads(template) if isinstance(template, str) else template) != expected_hash:
        raise ValueError("TLS stack template differs from plan")
    resources = cf.describe_stack_resources(StackName=stack_name)["StackResources"]
    owned = {item["LogicalResourceId"]: (item["PhysicalResourceId"], item["ResourceType"])
             for item in resources if item["ResourceType"] != "AWS::CDK::Metadata"}
    if any(item["ResourceStatus"] not in _COMPLETE for item in resources):
        raise ValueError("TLS resources must be complete")
    expected = {target.prefix + suffix: (physical, kind) for target in destinations for suffix, physical, kind in (
        ("TlsKey", target.key.rsplit("/", 1)[1], "AWS::KMS::Key"),
        ("TlsBundle", target.arn, "AWS::SecretsManager::Secret"),
    )}
    if owned != expected:
        raise ValueError("TLS resource ownership differs from plan")


def initialize(sdk, plan, progress, *, apply=False):
    stores = reviewed_plan(plan)
    cf, kms, sm = (sdk.client(name) for name in ("cloudformation", "kms", "secretsmanager"))
    token = plan["request_id"]
    candidate_stage = "foodtech-tonkatsu-trial-" + token
    required_stages = ("AWSCURRENT", candidate_stage)
    hosts = {name: plan[name] for name in ("broker_host", "gateway_host", "records_host", "inference_host") if name in plan}
    for stack, digest in plan["stacks"].items():
        progress["phase"] = "stack_metadata"
        _verify_stack(cf, stack, digest, [target for target in stores if target.stack == stack])

    def inspect_target(target):
        if sm.get_resource_policy(SecretId=target.arn).get("ResourcePolicy"):
            raise ValueError("unexpected TLS Secret resource policy")
        return verify_store(sm, kms, target, token, progress,
                            policy_factory=_expected_policy, required_stages=required_stages)

    def inspect_versions():
        progress["phase"] = "store_metadata"
        return [inspect_target(target) for target in stores]

    existing = inspect_versions()
    if not apply:
        return {"mode": "inspect", "request_id": token, "matching_versions": sum(existing), "values_read": False}
    if any(existing) and not all(existing):
        raise ValueError("partial TLS registration requires separate reconciliation")
    expected = None
    if not any(existing):
        progress["phase"] = "generate"
        material = generate(**hosts)
        expected = (material.business_api, material.broker, material.gateway)
        if material.inference is not None:
            expected += (material.inference,)
        for store, value in zip(stores, expected, strict=True):
            progress["phase"] = "write_" + store.role
            if inspect_target(store):
                raise ValueError("concurrent TLS registration requires reconciliation")
            write_value(sm, store, token, value, progress, candidate_stage=candidate_stage)
    if not all(inspect_versions()):
        raise ValueError("missing TLS version")
    progress["phase"] = "readback"
    recovered = tuple(read_value(sm, target, token, required_stages=required_stages) for target in stores)
    if expected is not None and recovered != expected:
        raise ValueError("stored TLS bundles differ")
    metadata = verify_bundles(*recovered, **hosts)
    return {"mode": "verified", "request_id": token, "recovered_existing": all(existing), "hosts": hosts,
            "versions": {target.role: {"arn": target.arn, "version_id": token} for target in stores}, **metadata}


def main(argv=None):
    try:
        parser = Arguments(description=__doc__)
        parser.add_argument("--reviewed-plan", type=Path, required=True)
        parser.add_argument("--apply", action="store_true")
        options = parser.parse_args(argv)
        if not hasattr(signal, "SIGALRM"):
            raise ValueError("this bounded operator command requires POSIX")
        verify_operator_environment()
        plan = json.loads(options.reviewed_plan.read_text())
        reviewed_plan(plan)
    except Exception:
        print("A valid reviewed Tonkatsu TLS initialization plan is required.", file=sys.stderr)
        return 2
    progress = {"phase": "identity", "attempted_secret_writes": 0, "acknowledged_secret_writes": 0,
                "observed_secret_versions": 0}
    try:
        with deadline(120):
            result = initialize(VerifiedOperator(), plan, progress, apply=options.apply)
    except (InitializationDeadline, KeyboardInterrupt, Exception) as error:
        print(json.dumps({"status": "not_verified", "failure_type": type(error).__name__, **progress,
                          "request_id": plan["request_id"], "reconciliation_required":
                              bool(progress["attempted_secret_writes"] or progress["observed_secret_versions"])}),
              file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
