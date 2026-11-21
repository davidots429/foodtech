"""Check that the synthesized REST task uses its role and secret references."""
import json
import sys


def main(path):
    with open(path, encoding='utf-8') as stream:
        resources = json.load(stream)['Resources']
    tasks = {name: item['Properties'] for name, item in resources.items()
             if item['Type'] == 'AWS::ECS::TaskDefinition'}
    if len(tasks) != 4:
        raise AssertionError('expected REST, MQTT and both OPC UA collection tasks')
    for prefix, expected, protocol in (
        ('RestCollectionTask', {'FOODTECH_MOCK_CLIENT_ID': 'client_id', 'FOODTECH_MOCK_CLIENT_SECRET': 'client_secret',
                                'FOODTECH_MOCK_CA_PEM': 'ca_pem'}, None),
        ('MqttCollectionTask', {'FOODTECH_MQTT_USER': 'username', 'FOODTECH_MQTT_PASSWORD': 'password',
                                'FOODTECH_MQTT_CLIENT_ID': 'client_id', 'FOODTECH_MOCK_CA_PEM': 'ca_pem'}, 'mqtt'),
        ('OpcUaJsonCollectionTask', {'FOODTECH_MOCK_CLIENT_ID': 'client_id', 'FOODTECH_MOCK_CLIENT_SECRET': 'client_secret',
                                     'FOODTECH_MOCK_CA_PEM': 'ca_pem'}, 'opcua-json'),
        ('OpcUaBinaryCollectionTask', {'FOODTECH_OPCUA_CLIENT_CERT_PEM': 'client_cert_pem',
                                       'FOODTECH_OPCUA_CLIENT_KEY_PEM': 'client_key_pem',
                                       'FOODTECH_OPCUA_SERVER_CERT_DER_BASE64': 'server_cert_der_base64'}, 'opcua-binary'),
    ):
        matches = [value for name, value in tasks.items() if name.startswith(prefix)]
        if len(matches) != 1:
            raise AssertionError('missing collection task ' + prefix)
        check_task(matches[0], expected, protocol)
    print('REST, MQTT and OPC UA task roles and secret references verified')


def check_task(task, expected, protocol):
    role = task['TaskRoleArn']['Fn::GetAtt'][0]
    if not role.startswith('CollectionAdapterTaskRole') or task['ExecutionRoleArn'] == task['TaskRoleArn']:
        raise AssertionError('task and execution roles are incorrect')
    container, = task['ContainerDefinitions']
    if container.get('User') != '10001' or task.get('NetworkMode') != 'awsvpc':
        raise AssertionError('task runtime settings changed')
    env = {item['Name'] for item in container.get('Environment', [])}
    secrets = {item['Name']: item['ValueFrom'] for item in container.get('Secrets', [])}
    if env.intersection(expected) or set(secrets) != set(expected):
        raise AssertionError('Mock credentials or CA are not injected as secrets')
    for name, field in expected.items():
        if not isinstance(secrets[name], str) or not secrets[name].startswith('arn:aws:secretsmanager:') or \
                not secrets[name].endswith(':' + field + '::'):
            raise AssertionError('invalid Mock secret reference')
    values = {item['Name']: item['Value'] for item in container.get('Environment', [])}
    if protocol and values.get('FOODTECH_COLLECTION_PROTOCOL') != protocol:
        raise AssertionError('collection task protocol is not selected')


if __name__ == '__main__':
    main(sys.argv[1])
