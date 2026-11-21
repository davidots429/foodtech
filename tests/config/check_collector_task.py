"""Check that the synthesized REST task uses its role and secret references."""
import json
import sys


def main(path):
    with open(path, encoding='utf-8') as stream:
        resources = json.load(stream)['Resources']
    tasks = [item['Properties'] for item in resources.values() if item['Type'] == 'AWS::ECS::TaskDefinition']
    if len(tasks) != 1:
        raise AssertionError('expected one collection task')
    task = tasks[0]
    role = task['TaskRoleArn']['Fn::GetAtt'][0]
    if not role.startswith('CollectionAdapterTaskRole') or task['ExecutionRoleArn'] == task['TaskRoleArn']:
        raise AssertionError('task and execution roles are incorrect')
    container, = task['ContainerDefinitions']
    if container.get('User') != '10001' or task.get('NetworkMode') != 'awsvpc':
        raise AssertionError('task runtime settings changed')
    env = {item['Name'] for item in container.get('Environment', [])}
    secrets = {item['Name']: item['ValueFrom'] for item in container.get('Secrets', [])}
    expected = {'FOODTECH_MOCK_CLIENT_ID': 'client_id', 'FOODTECH_MOCK_CLIENT_SECRET': 'client_secret',
                'FOODTECH_MOCK_CA_PEM': 'ca_pem'}
    if env.intersection(expected) or set(secrets) != set(expected):
        raise AssertionError('Mock credentials or CA are not injected as secrets')
    for name, field in expected.items():
        if not isinstance(secrets[name], str) or not secrets[name].startswith('arn:aws:secretsmanager:') or \
                not secrets[name].endswith(':' + field + '::'):
            raise AssertionError('invalid Mock secret reference')
    print('REST collection task role and secret references verified')


if __name__ == '__main__':
    main(sys.argv[1])
