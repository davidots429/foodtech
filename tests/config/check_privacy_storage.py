"""Check privacy-storage CloudFormation invariants; does not validate AWS IAM."""
import json
import sys
from pathlib import Path


def main():
    with open(sys.argv[1], encoding="utf-8") as stream:
        template = json.load(stream)
    with (Path(sys.argv[1]).parent / "manifest.json").open(encoding="utf-8") as stream:
        assembly = json.load(stream)
    assert assembly["artifacts"]["FoodtechPrivacyStorage-dev"]["properties"]["terminationProtection"] is True
    resources = template["Resources"]
    tables = {value["Properties"]["TableName"]: value for value in resources.values()
              if value["Type"] == "AWS::DynamoDB::Table"}
    expected = {"foodtech-identity-vault-dev", "foodtech-link-capsule-dev", "foodtech-deletion-registry-dev"}
    assert set(tables) == expected
    key_ids = set()
    for name, table in tables.items():
        properties = table["Properties"]
        assert table["DeletionPolicy"] == table["UpdateReplacePolicy"] == "Retain"
        assert properties["BillingMode"] == "PAY_PER_REQUEST" and properties["DeletionProtectionEnabled"] is True
        assert properties["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"},
                                           {"AttributeName": "sk", "KeyType": "RANGE"}]
        assert properties["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True, "RecoveryPeriodInDays": 7}
        assert "GlobalSecondaryIndexes" not in properties and "LocalSecondaryIndexes" not in properties
        sse = properties["SSESpecification"]
        assert sse["SSEEnabled"] is True and sse["SSEType"] == "KMS"
        key_id, attribute = sse["KMSMasterKeyId"]["Fn::GetAtt"]
        assert attribute == "Arn" and key_id not in key_ids
        key_ids.add(key_id)
        ttl = properties.get("TimeToLiveSpecification")
        assert ttl == ({"AttributeName": "ttl_delete_after", "Enabled": True}
                       if name == "foodtech-deletion-registry-dev" else None)
    keys = {name: value for name, value in resources.items() if value["Type"] == "AWS::KMS::Key"}
    assert len(keys) == 4 and key_ids <= set(keys)
    locator_key, attribute = template["Outputs"]["DeletionLocatorKeyArn"]["Value"]["Fn::GetAtt"]
    assert attribute == "Arn" and locator_key in keys and locator_key not in key_ids
    assert all(key["Properties"]["EnableKeyRotation"] is True and key["DeletionPolicy"] == "Retain"
               for key in keys.values())
    assert not any(value["Type"] == "AWS::IAM::Role" for value in resources.values())
    print("Privacy storage template structure verified; runtime IAM and restoration remain unverified.")


if __name__ == "__main__":
    main()
