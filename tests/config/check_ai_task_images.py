"""Synthesize both AI tasks; reject tags and invalid digest/repository inputs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from check_ai_principal_mode import main as check_principal_mode

ROOT = Path(__file__).resolve().parents[2]
DIGESTS = {'registrationImage': 'sha256:' + 'a' * 64, 'gatewayImage': 'sha256:' + 'b' * 64}
CONTEXT = {
    'provisionCredentialVault': 'true', 'provisionAiServiceTasks': 'true',
    'credentialKeyPeriod': '2026-11', 'credentialHmacVersion': 'synthetic-v1',
    'registrationImageRepository': 'foodtech/credential-registration',
    'gatewayImageRepository': 'foodtech/ai-gateway',
    'registrationTlsSecretArn': 'arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:synthetic-registration-aaaaaa',
    'gatewayTlsSecretArn': 'arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:synthetic-gateway-bbbbbb',
    **{name + 'Digest': value for name, value in DIGESTS.items()},
}


def synth(output, changes):
    context = {**CONTEXT, **changes}
    command = [str(ROOT / 'node_modules/.bin/cdk'), '--app', 'node --import tsx bin/app.ts',
               'synth', 'FoodtechAiServiceTasks-dev', '--strict', '--no-lookups', '--output', str(output)]
    for key, value in context.items():
        if value is not None:
            command.extend(['-c', f'{key}={value}'])
    return subprocess.run(command, cwd=ROOT / 'infra/cdk', capture_output=True, text=True,
                          env={**os.environ, 'CDK_DEFAULT_ACCOUNT': '123456789012'}, timeout=90)


def main():
    with tempfile.TemporaryDirectory(prefix='foodtech-ai-task-image-') as temp:
        for mode in ('false', 'true'):
            output = Path(temp) / mode
            result = synth(output, {'principalProvisioningEnabled': mode})
            assert result.returncode == 0, result.stderr
            path = output / 'FoodtechAiServiceTasks-dev.template.json'
            check_principal_mode(path, mode)
            template = json.loads(path.read_text())
            tasks = [r['Properties'] for r in template['Resources'].values() if r['Type'] == 'AWS::ECS::TaskDefinition']
            assert len(tasks) == 2
            found = {}
            for task in tasks:
                container, = task['ContainerDefinitions']
                service = next(e['Value'] for e in container['Environment'] if e['Name'] == 'FOODTECH_SERVICE')
                found[service] = container['Image']
            assert found == {service: {'Fn::Join': ['', [
                '123456789012.dkr.ecr.ap-southeast-2.', {'Ref': 'AWS::URLSuffix'},
                '/foodtech/' + service + '@' + DIGESTS[name]]]} for service, name in (
                    ('credential-registration', 'registrationImage'), ('ai-gateway', 'gatewayImage'))}
        for name in DIGESTS:
            for label, changes, error in (
                ('tag', {name + 'Tag': 'synthetic-test'}, name + 'Tag is not supported'),
                ('missing', {name + 'Digest': None}, name + 'Digest context is required'),
                ('latest', {name + 'Digest': 'latest'}, 'immutable sha256 digest'),
                ('short', {name + 'Digest': 'sha256:' + 'a' * 63}, 'immutable sha256 digest'),
                ('uppercase', {name + 'Digest': 'sha256:' + 'A' * 64}, 'immutable sha256 digest'),
                ('uri', {name + 'Repository': '123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/ai'}, 'repository name'),
                ('fips-uri', {name + 'Repository': '123456789012.dkr.ecr-fips.ap-southeast-2.amazonaws.com/foodtech/ai'}, 'repository name'),
                ('public-uri', {name + 'Repository': 'public.ecr.aws/synthetic/ai'}, 'repository name'),
            ):
                result = synth(Path(temp) / (name + label), changes)
                assert result.returncode != 0 and error in result.stderr, (name, label, result.stderr)
    print('Both AI tasks pinned by digest in both principal modes; 16 invalid/ambiguous inputs rejected.')


if __name__ == '__main__':
    main()
