"""Verify private REST trial deployment boundaries; never calls AWS."""
import argparse
from collections import Counter
import copy
import json
from pathlib import Path


def check(template):
    resources = template['Resources']
    def typed(kind):
        return {k: v for k, v in resources.items() if v['Type'] == kind}
    counts = Counter(v['Type'] for v in resources.values())
    counts.pop('AWS::CDK::Metadata', None)
    assert counts == Counter({'AWS::IAM::Role': 4, 'AWS::IAM::Policy': 3,
        'AWS::Logs::LogGroup': 2, 'AWS::ECS::TaskDefinition': 2,
        'AWS::S3::Bucket': 1, 'AWS::S3::BucketPolicy': 1})
    roles = typed('AWS::IAM::Role')
    by_name = {v['Properties']['RoleName']: k for k, v in roles.items()}
    assert set(by_name) == {'foodtech-collection-task-dev', 'foodtech-rest-mock-task-dev',
        'foodtech-private-rest-mock-execution-dev', 'foodtech-private-rest-collector-execution-dev'}
    collector, mock = (by_name[name] for name in ('foodtech-collection-task-dev', 'foodtech-rest-mock-task-dev'))
    bucket_id, = typed('AWS::S3::Bucket')
    object_resource = {'Fn::Join': ['', [{'Fn::GetAtt': [bucket_id, 'Arn']}, '/ingestion/*']]}
    policies = {v['Properties']['Roles'][0]['Ref']: v['Properties']['PolicyDocument']['Statement']
                for v in typed('AWS::IAM::Policy').values()}
    assert mock not in policies
    assert all('ManagedPolicyArns' not in v['Properties'] and 'Policies' not in v['Properties'] for v in roles.values())
    for role in roles.values():
        trust, = role['Properties']['AssumeRolePolicyDocument']['Statement']
        assert trust['Principal'] == {'Service': 'ecs-tasks.amazonaws.com'}
        assert trust['Action'] == 'sts:AssumeRole' and trust['Effect'] == 'Allow'
        assert set(trust['Condition']) == {'StringEquals', 'ArnLike'}
        account = trust['Condition']['StringEquals']['aws:SourceAccount']
        assert trust['Condition']['ArnLike']['aws:SourceArn'] == f'arn:aws:ecs:ap-southeast-2:{account}:*'
    def actions(statement):
        value = statement['Action']
        return set(value if isinstance(value, list) else [value])
    assert set.union(*(actions(s) for s in policies[collector])) == {
        'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query',
        'dynamodb:DeleteItem', 'sqs:SendMessage', 's3:PutObject'}
    for statement in policies[collector]:
        assert statement['Effect'] == 'Allow'
        assert statement['Condition']['StringEquals']['aws:RequestedRegion'] == 'ap-southeast-2'
        if any(a.startswith('dynamodb:') for a in actions(statement)):
            assert statement['Resource'] == f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-data-flow-dev'
            conditions = statement['Condition']
            assert conditions['Null'] == {'dynamodb:LeadingKeys': 'false'}
            expected = ['STREAM#*'] if 'dynamodb:DeleteItem' in actions(statement) else ['STREAM#*', 'MOCK#*', 'INGESTION_DLQ#*']
            assert conditions['ForAllValues:StringLike'] == {'dynamodb:LeadingKeys': expected}
        elif 'sqs:SendMessage' in actions(statement):
            assert statement['Resource'] == f'arn:aws:sqs:ap-southeast-2:{account}:foodtech-ordered-dev.fifo'
        elif 's3:PutObject' in actions(statement):
            assert statement['Resource'] == object_resource
            assert statement['Condition']['Bool'] == {'aws:SecureTransport': 'true'}
            assert statement['Condition']['StringEquals']['aws:ResourceAccount'] == account
            assert statement['Condition']['StringEquals']['s3:x-amz-server-side-encryption'] == 'AES256'
    parameters = template['Parameters']
    assert parameters['QuarantineWritesEnabled']['Default'] == 'false'
    assert parameters['QuarantineWritesEnabled']['AllowedValues'] == ['false', 'true']
    assert template['Conditions']['QuarantineWritesBlocked'] == {'Fn::Equals': [{'Ref': 'QuarantineWritesEnabled'}, 'false']}
    bucket, = typed('AWS::S3::Bucket').values()
    assert bucket['DeletionPolicy'] == bucket['UpdateReplacePolicy'] == 'Retain'
    props = bucket['Properties']
    assert props['BucketName'] == f'foodtech-ingestion-quarantine-{account}-dev'
    assert all(props['PublicAccessBlockConfiguration'].values())
    assert props['OwnershipControls']['Rules'] == [{'ObjectOwnership': 'BucketOwnerEnforced'}]
    assert props.get('VersioningConfiguration', {'Status': 'Suspended'})['Status'] == 'Suspended'
    assert props['BucketEncryption']['ServerSideEncryptionConfiguration'] == [
        {'ServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]
    bucket_policy, = typed('AWS::S3::BucketPolicy').values()
    tls, conditional = bucket_policy['Properties']['PolicyDocument']['Statement']
    assert tls['Effect'] == 'Deny' and tls['Action'] == 's3:*'
    assert tls['Condition'] == {'Bool': {'aws:SecureTransport': 'false'}}
    condition, denial, absent = conditional['Fn::If']
    assert condition == 'QuarantineWritesBlocked' and absent == {'Ref': 'AWS::NoValue'}
    assert denial['Effect'] == 'Deny' and denial['Action'] == 's3:PutObject'
    assert denial['Condition'] == {'ArnEquals': {'aws:PrincipalArn': {'Fn::GetAtt': [collector, 'Arn']}}}
    assert denial['Resource'] == object_resource
    for task in typed('AWS::ECS::TaskDefinition').values():
        props = task['Properties']
        assert props['NetworkMode'] == 'awsvpc' and props['RequiresCompatibilities'] == ['FARGATE']
        assert props['Cpu'] == '256' and props['Memory'] == '512'
        assert props['RuntimePlatform'] == {'CpuArchitecture': 'X86_64', 'OperatingSystemFamily': 'LINUX'}
        container, = props['ContainerDefinitions']
        name = container['Name']
        assert name in {'mock', 'collector'}
        expected_role = mock if name == 'mock' else collector
        execution = by_name[f'foodtech-private-rest-{name}-execution-dev']
        assert props['TaskRoleArn'] == {'Fn::GetAtt': [expected_role, 'Arn']}
        assert props['ExecutionRoleArn'] == {'Fn::GetAtt': [execution, 'Arn']}
        assert container['ReadonlyRootFilesystem'] is True and container['User'] == '10001:10001'
        assert container['LinuxParameters'] == {'Capabilities': {'Drop': ['ALL']}}
        assert container['Image'] == {'Ref': 'MockImageUri' if name == 'mock' else 'CollectorImageUri'}
        assert '@sha256:[a-f0-9]{64}' in parameters[container['Image']['Ref']]['AllowedPattern']
        assert 'Default' not in parameters[container['Image']['Ref']]
        env = {v['Name']: v['Value'] for v in container['Environment']}
        assert not {'FOODTECH_MOCK_BASE_URL', 'FOODTECH_REST_TRIAL_ATTEMPT', 'FOODTECH_REST_TRIAL_ID'} & env.keys()
        secret_parameter = 'MockSecretArn' if name == 'mock' else 'CollectorSecretArn'
        key_parameter = 'MockSecretKeyArn' if name == 'mock' else 'CollectorSecretKeyArn'
        assert 'Default' not in parameters[secret_parameter]
        for secret in container['Secrets']:
            assert {'Ref': secret_parameter} in secret['ValueFrom']['Fn::Join'][1]
        if name == 'collector':
            assert {v['Name'] for v in container['Secrets']} == {
                'FOODTECH_MOCK_CA_PEM', 'FOODTECH_MOCK_CLIENT_ID', 'FOODTECH_MOCK_CLIENT_SECRET'}
            assert container['Command'] == ['--trial']
            assert env['FOODTECH_REST_TRIAL_MAX_SECONDS'] == '60'
            assert props['Volumes'] == [{'Name': 'collector-ca'}]
            assert container['MountPoints'] == [{'ContainerPath': '/run/foodtech-collection', 'ReadOnly': False, 'SourceVolume': 'collector-ca'}]
        else:
            assert '--max-runtime' in container['Command'] and '600' in container['Command']
            command = container['Command']
            assert command[command.index('--bind-address') + 1] == '0.0.0.0'
            assert command[command.index('--fault') + 1] == 'invalid-utf8'
            assert command[command.index('--fixture') + 1] == 'fixtures/rest-invalid-utf8-single-v1.json'
            assert 'MountPoints' not in container
        expected_actions = {'ecr:GetAuthorizationToken', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer',
            'logs:CreateLogStream', 'logs:PutLogEvents', 'secretsmanager:GetSecretValue', 'kms:Decrypt'}
        assert set.union(*(actions(s) for s in policies[execution])) == expected_actions
        for statement in policies[execution]:
            assert statement['Effect'] == 'Allow'
            if 'ecr:GetAuthorizationToken' in actions(statement):
                assert statement['Resource'] == '*'
            elif 'ecr:BatchGetImage' in actions(statement):
                repository = 'mock' if name == 'mock' else 'collection'
                assert statement['Resource'] == f'arn:aws:ecr:ap-southeast-2:{account}:repository/foodtech/{repository}-dev'
            elif 'logs:PutLogEvents' in actions(statement):
                assert statement['Resource'] == f'arn:aws:logs:ap-southeast-2:{account}:log-group:/foodtech/dev/private-rest-{name}:log-stream:trial/{name}/*'
            if 'secretsmanager:GetSecretValue' in actions(statement):
                assert statement['Resource'] == {'Ref': secret_parameter}
                assert statement['Condition']['Bool'] == {'aws:SecureTransport': 'true'}
            if 'kms:Decrypt' in actions(statement):
                assert statement['Resource'] == {'Ref': key_parameter}
                assert statement['Condition']['StringEquals']['kms:ViaService'] == 'secretsmanager.ap-southeast-2.amazonaws.com'
                assert statement['Condition']['StringEquals']['kms:EncryptionContext:SecretARN'] == {'Ref': secret_parameter}
    for group in typed('AWS::Logs::LogGroup').values():
        assert group['Properties']['RetentionInDays'] == 7
    return True


def negative_checks(template):
    """Ensure removing each critical guard is detected by this checker."""
    mutations = []
    for key, resource in template['Resources'].items():
        if resource['Type'] == 'AWS::ECS::TaskDefinition':
            mutations += [(key, 'readonly'), (key, 'shared-role'), (key, 'shared-secret')]
            if resource['Properties']['ContainerDefinitions'][0]['Name'] == 'mock':
                mutations += [(key, 'loopback-mock'), (key, 'missing-fault')]
        elif resource['Type'] == 'AWS::S3::BucketPolicy':
            mutations += [(key, 'remove-denial'), (key, 'remove-tls')]
        elif resource['Type'] == 'AWS::S3::Bucket':
            mutations += [(key, 'public')]
        elif resource['Type'] == 'AWS::IAM::Policy':
            mutations += [(key, 'wildcard-resource')]
    for key, mutation in mutations:
        edited = copy.deepcopy(template)
        props = edited['Resources'][key]['Properties']
        if mutation == 'readonly': props['ContainerDefinitions'][0]['ReadonlyRootFilesystem'] = False
        elif mutation == 'shared-role': props['TaskRoleArn'] = props['ExecutionRoleArn']
        elif mutation == 'shared-secret': props['ContainerDefinitions'][0]['Secrets'][0]['ValueFrom'] = 'other-secret'
        elif mutation == 'remove-denial': props['PolicyDocument']['Statement'].pop()
        elif mutation == 'remove-tls': props['PolicyDocument']['Statement'][0]['Condition'] = {}
        elif mutation == 'public': props['PublicAccessBlockConfiguration']['BlockPublicPolicy'] = False
        elif mutation == 'wildcard-resource':
            statement = next(s for s in props['PolicyDocument']['Statement'] if s['Resource'] != '*')
            statement['Resource'] = '*'
        elif mutation == 'loopback-mock':
            command = props['ContainerDefinitions'][0]['Command']
            command[command.index('--bind-address') + 1] = '127.0.0.1'
        elif mutation == 'missing-fault':
            command = props['ContainerDefinitions'][0]['Command']
            index = command.index('--fault')
            del command[index:index + 2]
        try:
            check(edited)
        except (AssertionError, KeyError, TypeError, ValueError):
            continue
        raise AssertionError(f'unsafe mutation was accepted: {mutation}')
    return len(mutations)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('template')
    args = parser.parse_args()
    template = json.loads(Path(args.template).read_text())
    check(template)
    count = negative_checks(template)
    print(f'Private REST trial boundaries verified; {count} unsafe mutations rejected.')
