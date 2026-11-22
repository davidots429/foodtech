"""Require the same explicit principal mode in both AI service tasks."""
import json
import sys
from pathlib import Path


def main(path, expected):
    assert expected in {'true', 'false'}
    template = json.loads(Path(path).read_text())
    found = {}
    for resource in template['Resources'].values():
        if resource['Type'] != 'AWS::ECS::TaskDefinition':
            continue
        for container in resource['Properties']['ContainerDefinitions']:
            env = {item['Name']: item['Value'] for item in container.get('Environment', [])}
            service = env.get('FOODTECH_SERVICE')
            if service in {'credential-registration', 'ai-gateway'}:
                assert service not in found
                found[service] = env['FOODTECH_PRINCIPAL_PROVISIONING_ENABLED']
                if service == 'ai-gateway':
                    assert env['FOODTECH_EXTERNAL_EXECUTION_ENABLED'] == 'false'
    assert found == {'credential-registration': expected, 'ai-gateway': expected}


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
