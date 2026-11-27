"""Synthesize all collection protocols and reject mutable/ambiguous image inputs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
DIGEST = 'sha256:' + 'a' * 64
FLAGS = ['provisionRestCollectionTask', 'provisionMqttCollectionTask',
         'provisionOpcUaJsonCollectionTask', 'provisionOpcUaBinaryCollectionTask']
CONTEXT = {
    'provisionDataFlow': 'true', 'collectionImageRepository': 'foodtech/collection-dev',
    'collectionMockBaseUrl': 'https://mock.example.invalid',
    'collectionMqttHost': 'mock.example.invalid', 'collectionMqttPort': '8883',
    'collectionUaJsonBaseUrl': 'https://mock.example.invalid',
    'collectionUaBinaryEndpoint': 'opc.tcp://mock.example.invalid:4840',
}
for name in ['Mock', 'Mqtt', 'UaJson', 'UaBinary']:
    CONTEXT[f'collection{name}SecretArn'] = 'arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:synthetic-abcdef'


def synth(output, flags, image):
    context = {**CONTEXT, **{flag: 'true' for flag in flags}, **image}
    command = [str(ROOT / 'node_modules/.bin/cdk'), '--app', 'node --import tsx bin/app.ts',
               'synth', 'FoodtechMinimalDataFlow-dev', '--strict', '--no-lookups', '--output', str(output)]
    for key, value in context.items():
        command.extend(['-c', f'{key}={value}'])
    return subprocess.run(command, cwd=ROOT / 'infra/cdk', capture_output=True, text=True,
                          env={**os.environ, 'CDK_DEFAULT_ACCOUNT': '123456789012'}, timeout=90)


def main():
    with tempfile.TemporaryDirectory(prefix='foodtech-collection-image-') as temp:
        output = Path(temp) / 'valid'
        result = synth(output, FLAGS, {'collectionImageDigest': DIGEST})
        assert result.returncode == 0, result.stderr
        template = json.loads((output / 'FoodtechMinimalDataFlow-dev.template.json').read_text())
        tasks = [v['Properties'] for v in template['Resources'].values() if v['Type'] == 'AWS::ECS::TaskDefinition']
        assert len(tasks) == 4
        for task in tasks:
            container, = task['ContainerDefinitions']
            assert container['Image'] == {'Fn::Join': ['', [
                '123456789012.dkr.ecr.ap-southeast-2.', {'Ref': 'AWS::URLSuffix'},
                '/foodtech/collection-dev@' + DIGEST]]}
        for index, flag in enumerate(FLAGS):
            result = synth(Path(temp) / f'tag-{index}', [flag],
                           {'collectionImageDigest': DIGEST, 'collectionImageTag': 'v1'})
            assert result.returncode != 0 and 'collectionImageTag is not supported' in result.stderr
        for index, bad in enumerate(['latest', 'v1', 'sha256:' + 'a' * 63, 'sha256:' + 'A' * 64]):
            result = synth(Path(temp) / f'invalid-{index}', FLAGS[:1], {'collectionImageDigest': bad})
            assert result.returncode != 0 and 'immutable sha256 digest' in result.stderr
        result = synth(Path(temp) / 'missing', FLAGS[:1], {})
        assert result.returncode != 0 and 'collectionImageDigest context is required' in result.stderr
    print('Four digest-pinned collection tasks; tag, malformed and missing inputs rejected.')


if __name__ == '__main__':
    main()
