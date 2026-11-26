"""Verify opt-in identity worker deployment isolation and disabled schedule."""
import json
import re
import sys
from pathlib import Path


def evaluate(expression, parameters):
    if isinstance(expression, dict):
        if set(expression) == {'Ref'}:
            return parameters[expression['Ref']]
        if set(expression) == {'Fn::Equals'}:
            left, right = expression['Fn::Equals']
            return evaluate(left, parameters) == evaluate(right, parameters)
        if set(expression) == {'Fn::Not'}:
            return not evaluate(expression['Fn::Not'][0], parameters)
        if set(expression) == {'Fn::And'}:
            return all(evaluate(child, parameters) for child in expression['Fn::And'])
        raise AssertionError('unexpected role-separation expression')
    return expression


def main(path):
    template = json.loads(Path(path).read_text())
    resources = list(template['Resources'].values())
    task, = [r['Properties'] for r in resources if r['Type'] == 'AWS::ECS::TaskDefinition']
    container, = task['ContainerDefinitions']
    assert task['TaskRoleArn'] == {'Ref': 'TaskRoleArn'}
    assert task['ExecutionRoleArn'] == {'Ref': 'ExecutionRoleArn'}
    assert task['NetworkMode'] == 'awsvpc' and task['RequiresCompatibilities'] == ['FARGATE']
    assert container['EntryPoint'] == ['python', '-m', 'foodtech_business_api.withdrawal_identity_worker']
    assert container['Image'] == {'Ref': 'ImageUri'}
    assert container['User'] == '10001:10001' and container['ReadonlyRootFilesystem'] is True
    assert container['StopTimeout'] == 30
    env = {item['Name']: item['Value'] for item in container['Environment']}
    assert env == {'FOODTECH_WITHDRAWAL_IDENTITY_WORKER_ENABLED': 'true',
                   'FOODTECH_WITHDRAWAL_IDENTITY_TASK_ROLE_ARN': {'Ref': 'TaskRoleArn'},
                   'FOODTECH_WITHDRAWAL_IDENTITY_MAX_SECONDS': '600',
                   'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev',
                   'FOODTECH_DELETION_REGISTRY_TABLE': 'foodtech-deletion-registry-dev',
                   'FOODTECH_COGNITO_USER_POOL_ID': {'Ref': 'CognitoUserPoolId'}}
    rules = [r['Properties'] for r in resources if r['Type'] == 'AWS::Events::Rule']
    schedule, = [r for r in rules if 'ScheduleExpression' in r]
    assert schedule['State'] == 'DISABLED'
    assert schedule['Name'] == 'foodtech-withdrawal-identity-dev'
    target, = schedule['Targets']
    assert target['Id'] == 'IdentityRelease' and target['RoleArn'] == {'Ref': 'InvokeRoleArn'}
    assert target['DeadLetterConfig'] == {'Arn': {'Ref': 'DeliveryDlqArn'}}
    assert target['EcsParameters']['TaskCount'] == 1
    assert target['EcsParameters']['Group'] == 'foodtech-withdrawal-identity-dev'
    network = target['EcsParameters']['NetworkConfiguration']['AwsVpcConfiguration']
    assert network['AssignPublicIp'] == 'DISABLED'
    assert network['Subnets'] == {'Ref': 'SubnetIds'} and network['SecurityGroups'] == {'Ref': 'SecurityGroupIds'}
    stopped, = [r for r in rules if 'EventPattern' in r]
    assert stopped['EventPattern']['detail']['group'] == ['foodtech-withdrawal-identity-dev']
    assert stopped['EventPattern']['detail']['clusterArn'] == [{'Ref': 'ClusterArn'}]
    metric, = [r['Properties'] for r in resources if r['Type'] == 'AWS::Logs::MetricFilter']
    assert 'exitCode NOT EXISTS' in metric['FilterPattern']
    assert metric['MetricTransformations'][0]['MetricNamespace'] == 'Foodtech/WithdrawalIdentity'
    assert len([r for r in resources if r['Type'] == 'AWS::CloudWatch::Alarm']) == 1
    # This stack must not create task permissions, users or privacy storage.
    assert not any(r['Type'].startswith(('AWS::IAM::', 'AWS::Cognito::', 'AWS::DynamoDB::', 'AWS::KMS::')) for r in resources)
    parameters = template['Parameters']
    pattern = parameters['TaskRoleArn']['AllowedPattern']
    account = re.search(r'iam::([0-9]{12}):role/', pattern)[1]
    for name, kind in [('TaskRoleArn', 'task'), ('ExecutionRoleArn', 'execution'), ('InvokeRoleArn', 'invoke')]:
        role_pattern = parameters[name]['AllowedPattern']
        role = f'arn:aws:iam::{account}:role/foodtech-withdrawal-identity-{kind}-dev'
        assert re.fullmatch(role_pattern, role)
        assert not re.fullmatch(role_pattern, role.replace(account, '000000000000'))
        assert not re.fullmatch(role_pattern, role + '-other')
        assert not re.fullmatch(role_pattern, role.replace('-dev', '-demo'))
    for name in ('TaskRoleArn', 'ExecutionRoleArn', 'InvokeRoleArn', 'CognitoUserPoolId', 'ImageUri', 'IntervalMinutes'):
        assert 'Default' not in parameters[name]
    image_pattern = parameters['ImageUri']['AllowedPattern']
    image = '123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/worker'
    assert re.fullmatch(image_pattern, image + '@sha256:' + 'a' * 64)
    assert not re.fullmatch(image_pattern, image + ':latest')
    assert not re.fullmatch(parameters['CognitoUserPoolId']['AllowedPattern'], 'us-east-1_synthetic')
    assertions = template['Rules']['SeparateIdentityWorkerRoles']['Assertions']
    assert len(assertions) == 1
    for roles, allowed in ((('task', 'execution', 'invoke'), True), (('same', 'same', 'invoke'), False),
                           (('same', 'execution', 'same'), False), (('task', 'same', 'same'), False)):
        values = dict(zip(('TaskRoleArn', 'ExecutionRoleArn', 'InvokeRoleArn'), roles))
        assert evaluate(assertions[0]['Assert'], values) is allowed


if __name__ == '__main__':
    main(sys.argv[1])
