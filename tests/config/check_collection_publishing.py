"""Check collection image publication is explicit and commit-pinned."""
import json
import sys


def main(path):
    with open(path, encoding='utf-8') as stream:
        resources = json.load(stream)['Resources']
    repositories = [value['Properties'] for value in resources.values() if value['Type'] == 'AWS::ECR::Repository']
    projects = [value['Properties'] for value in resources.values() if value['Type'] == 'AWS::CodeBuild::Project']
    if len(repositories) != 1 or len(projects) != 1 or any(
            value['Type'] in {'AWS::CodeBuild::Webhook', 'AWS::Events::Rule'} for value in resources.values()):
        raise AssertionError('unexpected publishing resources')
    repo, project = repositories[0], projects[0]
    if repo['RepositoryName'] != 'foodtech/collection-adapter' or repo['ImageTagMutability'] != 'IMMUTABLE':
        raise AssertionError('collection repository configuration changed')
    source = project['Source']
    if source['Type'] != 'NO_SOURCE' or project.get('Triggers'):
        raise AssertionError('publishing requires explicit execution')
    spec = json.loads(source['BuildSpec'])
    pre = spec['phases']['pre_build']['commands']
    build = spec['phases']['build']['commands']
    required = ['grep -Eq', 'git clone --depth 1 --branch main', 'git -C source rev-parse HEAD',
                '"$SOURCE_SHA"', 'IMAGE_TAG=sha-']
    if not all(any(fragment in command for command in pre) for fragment in required):
        raise AssertionError('commit pinning is incomplete')
    if len(build) != 2 or 'collection-adapter.Dockerfile' not in build[0] or not build[1].startswith('docker push '):
        raise AssertionError('unexpected publishing build commands')
    print('Collection image publishing is explicit and commit-pinned')


if __name__ == '__main__':
    main(sys.argv[1])
