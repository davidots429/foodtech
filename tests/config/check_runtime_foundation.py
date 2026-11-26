"""Check the runtime network boundary; synth success does not prove deployment."""
import json
import sys
from collections import Counter
from pathlib import Path


def check(path):
    template = json.loads(Path(path).read_text())
    resources = template['Resources']
    counts = Counter(value['Type'] for value in resources.values())
    counts.pop('AWS::CDK::Metadata', None)
    assert counts == Counter({'AWS::EC2::VPC': 1, 'AWS::EC2::Subnet': 2,
        'AWS::EC2::RouteTable': 2, 'AWS::EC2::SubnetRouteTableAssociation': 2,
        'AWS::EC2::Route': 2, 'AWS::EC2::EIP': 1, 'AWS::EC2::NatGateway': 1,
        'AWS::EC2::InternetGateway': 1, 'AWS::EC2::VPCGatewayAttachment': 1,
        'AWS::EC2::VPCEndpoint': 2, 'AWS::EC2::SecurityGroup': 1,
        'AWS::ECS::Cluster': 1, 'AWS::ECR::Repository': 1})
    def of_type(kind):
        return {key: value for key, value in resources.items() if value['Type'] == kind}
    forbidden = ('AWS::IAM::', 'AWS::DynamoDB::', 'AWS::KMS::', 'AWS::Cognito::', 'AWS::Events::')
    assert not any(value['Type'].startswith(forbidden) for value in resources.values())
    assert not of_type('AWS::ECS::Service') and not of_type('AWS::ECS::TaskDefinition')
    zone = template['Parameters']['AvailabilityZone']
    assert zone['Type'] == 'AWS::EC2::AvailabilityZone::Name' and 'Default' not in zone
    assert zone['AllowedPattern'] == '^ap-southeast-2[a-z]$'
    assert len(of_type('AWS::EC2::VPC')) == len(of_type('AWS::EC2::NatGateway')) == 1
    assert len(of_type('AWS::EC2::EIP')) == 1
    subnets = of_type('AWS::EC2::Subnet')
    assert len(subnets) == 2
    assert all(value['Properties']['MapPublicIpOnLaunch'] is False for value in subnets.values())
    assert all(value['Properties']['AvailabilityZone'] == {'Ref': 'AvailabilityZone'} for value in subnets.values())
    routes = of_type('AWS::EC2::Route')
    assert len(routes) == 2
    private, = [value['Properties'] for value in routes.values() if 'NatGatewayId' in value['Properties']]
    public, = [value['Properties'] for value in routes.values() if 'GatewayId' in value['Properties']]
    assert private['DestinationCidrBlock'] == public['DestinationCidrBlock'] == '0.0.0.0/0'
    assert private['RouteTableId'] != public['RouteTableId']
    private_id = private['RouteTableId']['Ref']
    associations = of_type('AWS::EC2::SubnetRouteTableAssociation')
    assert len(associations) == 2
    assert sum(value['Properties']['RouteTableId'] == {'Ref': private_id} for value in associations.values()) == 1
    group, = of_type('AWS::EC2::SecurityGroup').values()
    assert not group['Properties'].get('SecurityGroupIngress')
    egress, = group['Properties']['SecurityGroupEgress']
    assert (egress['CidrIp'], egress['IpProtocol'], egress['FromPort'], egress['ToPort']) == ('0.0.0.0/0', 'tcp', 443, 443)
    assert not of_type('AWS::EC2::SecurityGroupIngress')
    endpoints = of_type('AWS::EC2::VPCEndpoint')
    assert len(endpoints) == 2
    policies = []
    for value in endpoints.values():
        props = value['Properties']
        assert props['VpcEndpointType'] == 'Gateway' and props['RouteTableIds'] == [{'Ref': private_id}]
        statement, = props['PolicyDocument']['Statement']
        assert statement['Effect'] == 'Allow'
        policies.append(statement)
    s3, = [statement for statement in policies if statement['Action'] == 's3:GetObject']
    assert 'prod-ap-southeast-2-starport-layer-bucket/*' in json.dumps(s3['Resource'])
    dynamo, = [statement for statement in policies if statement is not s3]
    assert set(dynamo['Action']) == {'dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:Scan',
        'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:ConditionCheckItem'}
    account = dynamo['Condition']['StringEquals']['aws:PrincipalAccount']
    assert account.isdigit() and len(account) == 12
    def text(value):
        if isinstance(value, str):
            return value
        if value == {'Ref': 'AWS::Partition'}:
            return 'aws'
        separator, parts = value['Fn::Join']
        return separator.join(text(part) for part in parts)
    names = {'auth-sessions', 'cooking-jobs', 'data-flow', 'ai-execution-registry',
        'ai-credential-vault', 'identity-vault', 'link-capsule', 'deletion-registry',
        'deletion-progress', 'tier-requests'}
    expected = {f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-{name}-dev{suffix}'
                for name in names for suffix in ('', '/index/*')}
    assert set(map(text, dynamo['Resource'])) == expected
    cluster, = of_type('AWS::ECS::Cluster').values()
    assert cluster['Properties']['ClusterName'] == 'foodtech-runtime-dev'
    repository, = of_type('AWS::ECR::Repository').values()
    props = repository['Properties']
    assert props['RepositoryName'] == 'foodtech/maintenance-dev'
    assert props['ImageTagMutability'] == 'IMMUTABLE'
    assert props['ImageScanningConfiguration']['ScanOnPush'] is True
    assert props.get('EncryptionConfiguration', {'EncryptionType': 'AES256'})['EncryptionType'] == 'AES256'
    assert props['EmptyOnDelete'] is False
    assert repository['DeletionPolicy'] == repository['UpdateReplacePolicy'] == 'Retain'
    assert {'VpcId', 'ClusterArn', 'PrivateSubnetIds', 'TaskSecurityGroupId', 'ImageRepositoryArn',
            'ImageRepositoryUri', 'DynamoDbEndpointId', 'ImageLayerEndpointId'} <= set(template['Outputs'])
    print('Runtime foundation network, image boundary and no-workload invariants verified.')


if __name__ == '__main__':
    check(sys.argv[1])
