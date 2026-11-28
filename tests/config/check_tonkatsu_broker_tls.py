"""Verify the empty TLS bundle and its dedicated key against the Task contract."""
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import sys


def check(tls, task, execution, account):
    resources = {name: resource for name, resource in tls["Resources"].items() if name != "CDKMetadata"}
    assert {name: resource["Type"] for name, resource in resources.items()} == {
        "BrokerTlsKey": "AWS::KMS::Key", "BrokerTlsBundle": "AWS::SecretsManager::Secret",
    }
    key, secret = resources["BrokerTlsKey"], resources["BrokerTlsBundle"]
    for resource in (key, secret):
        assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain"

    bundle = secret["Properties"]
    assert set(bundle) == {"Name", "Description", "KmsKeyId", "Tags"}
    assert bundle["Name"] == "foodtech/tonkatsu-broker-dev/tls"
    key_arn = {"Fn::GetAtt": ["BrokerTlsKey", "Arn"]}
    assert bundle["KmsKeyId"] == key_arn
    assert tls["Outputs"] == {
        "BrokerTlsBundleArn": {"Value": {"Ref": "BrokerTlsBundle"}},
        "BrokerTlsKeyArn": {"Value": key_arn},
    }

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

    for template in (task, execution):
        pattern = template["Parameters"]["BrokerTlsBundleArn"]["AllowedPattern"]
        assert re.fullmatch(pattern, context + "-aB1234")
        assert not re.fullmatch(pattern, context + "-other-aB1234")


if __name__ == "__main__":
    tls, task, execution = (json.loads(Path(path).read_text()) for path in sys.argv[1:4])
    check(tls, task, execution, sys.argv[4])
    print("Empty Tonkatsu broker TLS store, dedicated key and Task ARN contracts verified")
