"""Verify the empty TLS bundle and its dedicated key against the Task contract."""
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import sys


def check_store(tls, prefix, name, account):
    resources = tls["Resources"]
    key_id, bundle_id = f"{prefix}TlsKey", f"{prefix}TlsBundle"
    key, secret = resources[key_id], resources[bundle_id]
    assert key["Type"] == "AWS::KMS::Key" and secret["Type"] == "AWS::SecretsManager::Secret"
    for resource in (key, secret):
        assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain"

    bundle = secret["Properties"]
    assert set(bundle) == {"Name", "Description", "KmsKeyId", "Tags"}
    assert bundle["Name"] == name
    key_arn = {"Fn::GetAtt": [key_id, "Arn"]}
    assert bundle["KmsKeyId"] == key_arn
    assert tls["Outputs"][f"{prefix}TlsBundleArn"]["Value"] == {"Ref": bundle_id}
    assert tls["Outputs"][f"{prefix}TlsKeyArn"]["Value"] == key_arn

    settings = key["Properties"]
    assert settings["Enabled"] and settings["EnableKeyRotation"]
    assert settings["MultiRegion"] is False and settings["PendingWindowInDays"] == 7
    assert settings["KeySpec"] == "SYMMETRIC_DEFAULT" and settings["KeyUsage"] == "ENCRYPT_DECRYPT"
    assert not settings.get("BypassPolicyLockoutSafetyCheck", False)

    admin, crypto = settings["KeyPolicy"]["Statement"]
    for statement in (admin, crypto):
        assert statement["Effect"] == "Allow" and statement["Resource"] == "*"
        assert statement["Principal"] == {"AWS": f"arn:aws:iam::{account}:root"}
    for operation in ("kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:ReEncryptFrom", "kms:Sign"):
        assert not any(fnmatchcase(operation, action) for action in admin["Action"])
    assert crypto["Action"] == ["kms:Decrypt", "kms:GenerateDataKey"]
    context = f"arn:aws:secretsmanager:ap-southeast-2:{account}:secret:{bundle['Name']}"
    assert crypto["Condition"] == {
        "StringEquals": {
            "kms:ViaService": "secretsmanager.ap-southeast-2.amazonaws.com", "kms:CallerAccount": account,
        },
        "StringLike": {"kms:EncryptionContext:SecretARN": context + "-??????"},
    }

    return context


def check_broker(tls, task, execution, account):
    assert set(tls["Resources"]) - {"CDKMetadata"} == {"BrokerTlsKey", "BrokerTlsBundle"}
    assert set(tls["Outputs"]) == {"BrokerTlsBundleArn", "BrokerTlsKeyArn"}
    context = check_store(tls, "Broker", "foodtech/tonkatsu-broker-dev/tls", account)
    for template in (task, execution):
        pattern = template["Parameters"]["BrokerTlsBundleArn"]["AllowedPattern"]
        assert re.fullmatch(pattern, context + "-aB1234")
        assert not re.fullmatch(pattern, context + "-other-aB1234")


def check_peers(tls, account):
    prefixes = {"BusinessApi": "foodtech/tonkatsu-business-api-dev/tls", "Gateway": "foodtech/tonkatsu-gateway-dev/tls"}
    assert set(tls["Resources"]) - {"CDKMetadata"} == {
        f"{prefix}{resource}" for prefix in prefixes for resource in ("TlsKey", "TlsBundle")
    }
    assert set(tls["Outputs"]) == {
        f"{prefix}{output}" for prefix in prefixes for output in ("TlsKeyArn", "TlsBundleArn")
    }
    contexts = [check_store(tls, prefix, name, account) for prefix, name in prefixes.items()]
    for own in contexts:
        for candidate in contexts:
            assert fnmatchcase(candidate + "-aB1234", own + "-??????") == (own == candidate)


if __name__ == "__main__":
    tls, task, execution = (json.loads(Path(path).read_text()) for path in sys.argv[1:4])
    check_broker(tls, task, execution, sys.argv[4])
    check_peers(json.loads(Path(sys.argv[5]).read_text()), sys.argv[4])
    if len(sys.argv) == 7:
        inference = json.loads(Path(sys.argv[6]).read_text())
        assert set(inference["Resources"]) - {"CDKMetadata"} == {"InferenceTlsKey", "InferenceTlsBundle"}
        assert set(inference["Outputs"]) == {"InferenceTlsBundleArn", "InferenceTlsKeyArn"}
        check_store(inference, "Inference", "foodtech/tonkatsu-inference-dev/tls", sys.argv[4])
    print("Empty Tonkatsu TLS stores, dedicated keys and broker Task ARN contracts verified")
