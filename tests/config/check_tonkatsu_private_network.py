"""Check the synthesized private demo network and its managed-login variant."""

import json
from pathlib import Path
import sys


def validate(template, managed_login):
    resources = template["Resources"]

    def rows(resource_type):
        return [row["Properties"] for row in resources.values() if row["Type"] == resource_type]

    subnets = rows("AWS::EC2::Subnet")
    assert len(subnets) == (3 if managed_login else 1)
    assert all(subnet.get("MapPublicIpOnLaunch", False) is False for subnet in subnets)
    assert len(rows("AWS::EC2::NatGateway")) == len(rows("AWS::EC2::InternetGateway")) == int(managed_login)
    assert len(rows("AWS::EC2::EIP")) == int(managed_login)
    assert len(rows("AWS::EC2::Route")) == (2 if managed_login else 0)
    assert all(row["DestinationCidrBlock"] == "0.0.0.0/0" for row in rows("AWS::EC2::Route"))
    assert len(rows("AWS::ECS::Cluster")) == 1
    assert not rows("AWS::ECS::Service") and not rows("AWS::ECS::TaskDefinition")
    assert not rows("AWS::ElasticLoadBalancingV2::LoadBalancer")

    for group in rows("AWS::EC2::SecurityGroup"):
        assert not group.get("SecurityGroupIngress")
        assert group["SecurityGroupEgress"] == [{
            "CidrIp": "255.255.255.255/32", "IpProtocol": "icmp", "FromPort": 252, "ToPort": 86,
            "Description": "Suppress default allow-all egress",
        }]
    edges = {
        ("Tester", "Business", 8080), ("Business", "Broker", 8446),
        ("Business", "Gateway", 8447), ("Broker", "Gateway", 8445),
        ("Gateway", "Inference", 8448), ("Broker", "WorkflowAccess", 443),
        ("Gateway", "WorkflowAccess", 443),
    }
    tasks = ("Business", "Broker", "Gateway", "Inference")
    edges.update((task, "ImageAccess", 443) for task in tasks)
    if managed_login:
        edges.add(("Business", "AuthAccess", 443))

    incoming = rows("AWS::EC2::SecurityGroupIngress")
    outgoing = rows("AWS::EC2::SecurityGroupEgress")
    assert all(row["IpProtocol"] == "tcp" and row["FromPort"] == row["ToPort"]
               for row in incoming + outgoing)
    assert all("SourceSecurityGroupId" in row for row in incoming)

    def group_id(row, key):
        return row[key]["Fn::GetAtt"][0]

    assert {(group_id(r, "SourceSecurityGroupId"), group_id(r, "GroupId"), r["FromPort"])
            for r in incoming} == edges
    assert {(group_id(r, "GroupId"), group_id(r, "DestinationSecurityGroupId"), r["FromPort"])
            for r in outgoing if "DestinationSecurityGroupId" in r} == edges
    prefix_rules = {(group_id(r, "GroupId"), r["DestinationPrefixListId"]["Ref"], r["FromPort"])
                    for r in outgoing if "DestinationPrefixListId" in r}
    assert prefix_rules == ({(task, "S3PrefixListId", 443) for task in tasks} |
                            {(task, "DynamoDbPrefixListId", 443) for task in tasks[:-1]})
    cidr_rules = {(group_id(r, "GroupId"), r["CidrIp"], r["FromPort"]) for r in outgoing if "CidrIp" in r}
    assert cidr_rules == ({("Business", "0.0.0.0/0", 443)} if managed_login else set())
    assert not any("CidrIpv6" in row for row in outgoing)

    endpoints = rows("AWS::EC2::VPCEndpoint")
    interface = {row["ServiceName"].split("ap-southeast-2.")[-1]: row for row in endpoints
                 if row["VpcEndpointType"] == "Interface"}
    expected = {"ecr.api", "ecr.dkr", "logs", "secretsmanager", "sts"}
    assert set(interface) == expected | ({"kms"} if managed_login else set())
    assert all(row["PrivateDnsEnabled"] and len(row["SubnetIds"]) == 1 for row in interface.values())
    gateways = {row["ServiceName"]["Fn::Join"][1][-1]: row for row in endpoints
                if row["VpcEndpointType"] == "Gateway"}
    assert set(gateways) == {".s3", ".dynamodb"}
    assert all(len(row["RouteTableIds"]) == (2 if managed_login else 1) for row in gateways.values())
    assert all(row["PolicyDocument"]["Statement"] for row in endpoints)
    assert gateways[".s3"]["PolicyDocument"]["Statement"] == [{
        "Action": "s3:GetObject", "Effect": "Allow", "Principal": {"AWS": "*"},
        "Resource": "arn:aws:s3:::prod-ap-southeast-2-starport-layer-bucket/*",
    }]
    iam_rows = [statement for endpoint in endpoints if endpoint is not gateways[".s3"]
                for statement in endpoint["PolicyDocument"]["Statement"]]
    assert all(statement["Condition"] == {"StringEquals": {"aws:PrincipalAccount": "111122223333"}}
               for statement in iam_rows)
    for service in ("ecr.api", "ecr.dkr"):
        for rule in interface[service]["PolicyDocument"]["Statement"]:
            if rule["Resource"] == "*":
                assert rule["Action"] == "ecr:GetAuthorizationToken"
            else:
                assert set(rule["Resource"]) == {
                    f"arn:aws:ecr:ap-southeast-2:111122223333:repository/foodtech/tonkatsu-{name}-dev"
                    for name in ("broker", "gateway", "ollama")
                }
    assert rows("AWS::ServiceDiscovery::PrivateDnsNamespace")[0]["Name"] == "tonkatsu.foodtech.internal"
    assert {row["Name"] for row in rows("AWS::ServiceDiscovery::Service")} == {
        "business-api", "broker", "gateway", "inference",
    }
    assert template["Outputs"]["BusinessSubnetId"] != template["Outputs"]["IsolatedSubnetId"] if managed_login else (
        template["Outputs"]["BusinessSubnetId"] == template["Outputs"]["IsolatedSubnetId"]
    )


if __name__ == "__main__":
    validate(json.loads(Path(sys.argv[1]).read_text()), managed_login=False)
    validate(json.loads(Path(sys.argv[2]).read_text()), managed_login=True)
    print("Tonkatsu private network contracts passed in both modes")
