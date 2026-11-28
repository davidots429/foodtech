"""Verify that each publisher only targets its private registry."""
import json
from pathlib import Path
import sys


def validate(template, service="broker"):
    resources = {name: resource for name, resource in template["Resources"].items() if name != "CDKMetadata"}
    assert sorted(resource["Type"] for resource in resources.values()) == [
        "AWS::ECR::Repository", "AWS::IAM::Policy", "AWS::IAM::Role",
    ]
    ecr_id, ecr = next((name, resource) for name, resource in resources.items()
                       if resource["Type"] == "AWS::ECR::Repository")
    assert ecr["DeletionPolicy"] == ecr["UpdateReplacePolicy"] == "Retain"
    assert ecr["Properties"]["RepositoryName"] == f"foodtech/tonkatsu-{service}-dev"
    assert ecr["Properties"]["ImageTagMutability"] == "IMMUTABLE"
    assert ecr["Properties"]["EncryptionConfiguration"] == {"EncryptionType": "AES256"}
    assert ecr["Properties"]["ImageScanningConfiguration"] == {"ScanOnPush": True}
    role_id, role = next((name, resource["Properties"]) for name, resource in resources.items()
                         if resource["Type"] == "AWS::IAM::Role")
    assert role["RoleName"] == f"foodtech-tonkatsu-{service}-publish-dev" and role["MaxSessionDuration"] == 3600
    assert "ManagedPolicyArns" not in role and "Policies" not in role
    assert role["AssumeRolePolicyDocument"]["Statement"] == [{
        "Action": "sts:AssumeRoleWithWebIdentity", "Effect": "Allow",
        "Principal": {"Federated": {"Ref": "GitHubOidcProviderArn"}},
        "Condition": {"StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": {"Ref": "VerifiedGitHubSubject"},
        }},
    }]
    policy = next(resource["Properties"] for resource in resources.values() if resource["Type"] == "AWS::IAM::Policy")
    assert policy["Roles"] == [{"Ref": role_id}]
    region = {"StringEquals": {"aws:RequestedRegion": "ap-southeast-2"}}
    assert policy["PolicyDocument"]["Statement"] == [
        {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*", "Condition": region},
        {"Effect": "Allow", "Action": [
            "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
            "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages",
            "ecr:DescribeRepositories",
        ], "Resource": {"Fn::GetAtt": [ecr_id, "Arn"]}, "Condition": region},
    ]
    parameters = template["Parameters"]
    assert "Default" not in parameters["GitHubOidcProviderArn"]
    subject = parameters["VerifiedGitHubSubject"]
    assert "Default" not in subject and subject["AllowedValues"] == [
        "repo:davidots429/foodtech:ref:refs/heads/main",
        "repo:davidots429@182712727/foodtech@1439543152:ref:refs/heads/main",
    ]


if __name__ == "__main__":
    validate(json.loads(Path(sys.argv[1]).read_text()))
    if len(sys.argv) == 3:
        validate(json.loads(Path(sys.argv[2]).read_text()), "gateway")
    print("Tonkatsu publishing definitions verified")
