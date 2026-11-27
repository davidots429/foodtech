"""Public metadata for manual main-only collection image publishing."""
import argparse
import json
import os
from pathlib import Path
import re

import maintenance_publish as common

WORKFLOW = common.REPOSITORY + '/.github/workflows/publish-collection-image.yml@refs/heads/main'


def target(env):
    sha = common.context(env)
    role = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-collection-publish-dev', env.get('PUBLISH_ROLE_ARN', ''))
    if not role or role[1] != env.get('PUBLISH_ACCOUNT_ID'):
        raise ValueError('reviewed collection publishing identity required')
    registry = role[1] + '.dkr.ecr.ap-southeast-2.amazonaws.com'
    return {'account': role[1], 'registry': registry, 'repository_name': 'foodtech/collection-dev',
        'repository': registry + '/foodtech/collection-dev', 'workload': 'collection',
        'tag': f'sha-{sha}-run-{env["GITHUB_RUN_ID"]}-{env["GITHUB_RUN_ATTEMPT"]}'}


def manifest(env, digest):
    values = target(env)
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid collection registry digest')
    return {'schema': 1, 'workload': 'collection', 'source_sha': common.context(env),
        'image_uri': values['repository'] + '@' + digest, 'image_tag': values['tag'], 'digest': digest,
        'platform': 'linux/amd64', 'workflow_run_id': env['GITHUB_RUN_ID'], 'workflow_attempt': env['GITHUB_RUN_ATTEMPT']}


def verify_repository(env, document):
    """Reject an unexpected repository or mutable destination before Docker login."""
    values = target(env)
    repositories = document.get('repositories', [])
    if len(repositories) != 1:
        raise ValueError('one reviewed collection repository required')
    repository = repositories[0]
    if repository.get('registryId') != values['account'] or \
            repository.get('repositoryName') != values['repository_name'] or \
            repository.get('repositoryUri') != values['repository'] or \
            repository.get('repositoryArn') != f'arn:aws:ecr:ap-southeast-2:{values["account"]}:repository/{values["repository_name"]}' or \
            repository.get('imageTagMutability') != 'IMMUTABLE' or \
            repository.get('encryptionConfiguration', {}).get('encryptionType') != 'AES256' or \
            repository.get('imageScanningConfiguration', {}).get('scanOnPush') is not True:
        raise ValueError('collection destination settings differ from reviewed contract')
    return {'repository_verified': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['inspect', 'target', 'verify-repository', 'manifest'])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--input', type=Path)
    options = parser.parse_args()
    try:
        if options.mode == 'inspect':
            values = common.inspect_claims(common.fetch_token(os.environ), os.environ, expected_workflow=WORKFLOW)
            values['inspection_only'] = True
        elif options.mode == 'target':
            values = target(os.environ)
        elif options.mode == 'verify-repository':
            if options.input is None:
                raise ValueError('repository metadata input required')
            values = verify_repository(os.environ, json.loads(options.input.read_text()))
        else:
            values = manifest(os.environ, os.environ.get('IMAGE_DIGEST', ''))
        if options.output:
            options.output.write_text(json.dumps(values, indent=2) + '\n')
        if options.mode == 'target':
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
                for key, value in values.items():
                    stream.write(f'{key}={value}\n')
        print(json.dumps(values, sort_keys=True))
        return 0
    except Exception:
        print('Collection publishing metadata check failed; credential and token values were not printed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
