"""Manual publishing metadata. Never prints tokens or authenticates by decoding JWTs.

The inspect mode reads public claims from the trusted GitHub token response;
AWS STS still verifies the signed token when the publishing role is assumed.
"""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = 'davidots429/foodtech'
REPOSITORY_ID = '1439543152'
OWNER_ID = '182712727'
WORKFLOW = REPOSITORY + '/.github/workflows/publish-maintenance-image.yml@refs/heads/main'
REGION = 'ap-southeast-2'


def context(env):
    required = {'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_REPOSITORY_ID': REPOSITORY_ID,
        'GITHUB_REPOSITORY_OWNER_ID': OWNER_ID, 'GITHUB_REF': 'refs/heads/main',
        'GITHUB_EVENT_NAME': 'workflow_dispatch'}
    if any(env.get(key) != value for key, value in required.items()):
        raise ValueError('manual main-branch context required')
    sha = env.get('GITHUB_SHA', '')
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('invalid source commit')
    for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        if not re.fullmatch('[1-9][0-9]{0,19}', env.get(key, '')):
            raise ValueError('invalid run identity')
    return sha


def inspect_claims(token, env, now=None):
    sha = context(env)
    if not isinstance(token, str) or len(token) > 65536 or token.count('.') != 2:
        raise ValueError('invalid token response')
    header, payload, signature = token.split('.')
    if not header or not signature:
        raise ValueError('invalid token response')
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    expected = {'iss': 'https://token.actions.githubusercontent.com', 'aud': 'sts.amazonaws.com',
        'repository': REPOSITORY, 'repository_id': REPOSITORY_ID, 'repository_owner_id': OWNER_ID,
        'ref': 'refs/heads/main', 'sha': sha, 'event_name': 'workflow_dispatch', 'workflow_ref': WORKFLOW}
    if any(claims.get(key) != value for key, value in expected.items()):
        raise ValueError('workflow claims do not match context')
    legacy = f'repo:{REPOSITORY}:ref:refs/heads/main'
    immutable = f'repo:davidots429@{OWNER_ID}/foodtech@{REPOSITORY_ID}:ref:refs/heads/main'
    if claims.get('sub') not in (legacy, immutable):
        raise ValueError('unrecognized repository subject')
    instant = time.time() if now is None else now
    for key in ('nbf', 'exp'):
        value = claims.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('invalid token lifetime')
    if not claims['nbf'] <= instant < claims['exp']:
        raise ValueError('token outside validity period')
    return {key: claims[key] for key in ('sub', 'aud', 'repository_id', 'repository_owner_id', 'ref', 'sha')}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('token endpoint redirect refused')


def fetch_token(env):
    context(env)
    url = urlsplit(env['ACTIONS_ID_TOKEN_REQUEST_URL'])
    if url.scheme != 'https' or not url.hostname or not url.hostname.endswith('.actions.githubusercontent.com') or url.username or url.password:
        raise ValueError('unexpected token endpoint')
    query = [(key, value) for key, value in parse_qsl(url.query) if key != 'audience']
    query.append(('audience', 'sts.amazonaws.com'))
    target = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ''))
    request = Request(target, headers={'Authorization': 'bearer ' + env['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('oversized token response')
    return json.loads(raw)['value']


def target(env):
    sha = context(env)
    match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-maintenance-publish-dev', env.get('PUBLISH_ROLE_ARN', ''))
    if not match:
        raise ValueError('reviewed publishing role is required')
    account = match[1]
    if account != env.get('PUBLISH_ACCOUNT_ID'):
        raise ValueError('publishing account does not match reviewed configuration')
    registry = f'{account}.dkr.ecr.{REGION}.amazonaws.com'
    return {'account': account, 'registry': registry, 'repository': registry + '/foodtech/maintenance-dev',
            'tag': f'sha-{sha}-run-{env["GITHUB_RUN_ID"]}-{env["GITHUB_RUN_ATTEMPT"]}'}


def manifest(env, digest):
    values = target(env)
    if not re.fullmatch('sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid registry image digest')
    return {'schema': 1, 'source_sha': context(env), 'image_uri': values['repository'] + '@' + digest,
        'image_tag': values['tag'], 'digest': digest, 'platform': 'linux/amd64',
        'workflow_run_id': env['GITHUB_RUN_ID'], 'workflow_attempt': env['GITHUB_RUN_ATTEMPT']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['inspect', 'target', 'manifest'])
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    try:
        if options.mode == 'inspect':
            values = inspect_claims(fetch_token(os.environ), os.environ)
            values['inspection_only'] = True
        elif options.mode == 'target':
            values = target(os.environ)
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
        print('Maintenance publishing metadata check failed; credentials and token values were not printed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
