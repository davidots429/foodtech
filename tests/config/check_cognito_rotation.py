"""Check the synthesized identity stack, without accessing AWS."""
import json
import sys
from pathlib import Path


def main(path: str) -> None:
    template = json.loads(Path(path).read_text())
    clients = [item['Properties'] for item in template['Resources'].values()
               if item['Type'] == 'AWS::Cognito::UserPoolClient']
    if len(clients) != 2:
        raise AssertionError('Expected customer and admin app clients')
    for client in clients:
        if client.get('RefreshTokenRotation') != {'Feature': 'ENABLED', 'RetryGracePeriodSeconds': 10}:
            raise AssertionError('Refresh token rotation is not configured')
        if client.get('EnableTokenRevocation') is not True:
            raise AssertionError('Token revocation is not enabled')
        if 'ALLOW_REFRESH_TOKEN_AUTH' in client.get('ExplicitAuthFlows', []):
            raise AssertionError('Refresh token auth is incompatible with rotation')


if __name__ == '__main__':
    main(sys.argv[1])
