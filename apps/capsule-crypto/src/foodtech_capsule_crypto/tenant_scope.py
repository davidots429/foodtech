"""Derive a recoverable, purpose- and period-scoped Capsule tenant value.

The caller must obtain the internal ID from the authenticated Vault boundary
and persist the descriptor with the wrapped key. This is not an authorizer or
key selector; a trusted caller supplies the approved purpose/version key map.
"""
import base64
import re
from dataclasses import dataclass
from uuid import UUID

import boto3
from .contract import validate_tenant


@dataclass(frozen=True)
class TenantScope:
    purpose: str
    epoch: str
    key_version: str

    def validate(self, approved_purposes):
        if not isinstance(self.purpose, str) or self.purpose not in approved_purposes or \
                not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', self.purpose) or \
                not isinstance(self.epoch, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', self.epoch) or \
                self.epoch.startswith('0000-') or not isinstance(self.key_version, str) or \
                not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', self.key_version):
            raise ValueError('invalid capsule tenant scope')


class TenantScopeResolver:
    def __init__(self, *, key_map, approved_purposes, kms_client=None):
        if not isinstance(key_map, dict) or not key_map or not approved_purposes:
            raise ValueError('trusted tenant scope configuration required')
        self.key_map, self.approved_purposes = dict(key_map), frozenset(approved_purposes)
        self.kms = kms_client or boto3.client('kms', region_name='ap-southeast-2')

    def derive(self, *, internal_id, scope: TenantScope) -> str:
        try:
            if not isinstance(scope, TenantScope) or not isinstance(internal_id, bytearray):
                raise ValueError
            scope.validate(self.approved_purposes)
            identity = internal_id.decode('ascii')
            if str(UUID(identity)) != identity:
                raise ValueError
            arn = self.key_map[(scope.purpose, scope.key_version)]
            if not isinstance(arn, str) or not re.fullmatch(
                    r'arn:aws:kms:ap-southeast-2:[0-9]{12}:key/[A-Za-z0-9-]+', arn):
                raise ValueError
            values = ('v1', scope.purpose, scope.epoch, scope.key_version, identity)
            message = b''.join(len(value.encode('utf-8')).to_bytes(4, 'big') + value.encode('utf-8') for value in values)
            response = self.kms.generate_mac(KeyId=arn, Message=message, MacAlgorithm='HMAC_SHA_256')
            mac = response.get('Mac')
            if response.get('KeyId') != arn or not isinstance(mac, bytes) or len(mac) != 32 or \
                    response.get('MacAlgorithm') != 'HMAC_SHA_256':
                raise ValueError
            digest = base64.urlsafe_b64encode(mac).rstrip(b'=').decode('ascii')
            tenant = f'psn:v1:{scope.purpose}:{scope.epoch}:{scope.key_version}:{digest}'
            validate_tenant(tenant, self.approved_purposes)
            return tenant
        except Exception:
            raise ValueError('unable to derive capsule tenant scope') from None
