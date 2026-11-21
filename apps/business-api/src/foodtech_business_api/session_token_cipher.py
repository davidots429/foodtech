"""Encrypt refresh tokens for a single server session, without logging them.

This primitive does not create or renew sessions; callers must provide an atomic
rotation protocol and enforce revocation before enabling refresh.
"""
import hashlib

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateAwsKmsKeyringInput
from aws_encryption_sdk import CommitmentPolicy
from aws_encryption_sdk.identifiers import Algorithm

SUITE = Algorithm.AES_256_GCM_HKDF_SHA512_COMMIT_KEY_ECDSA_P384
MAX_TOKEN_BYTES = 16_384
MAX_CIPHERTEXT_BYTES = 65_536


class SessionTokenCipher:
    def __init__(self, key_arn: str, environment: str, *, kms_client=None, sdk=None, providers=None):
        if not isinstance(key_arn, str) or not key_arn.startswith('arn:aws:kms:') or \
                not isinstance(environment, str) or not environment or len(environment) > 64:
            raise ValueError('invalid session key configuration')
        self.key_arn, self.environment = key_arn, environment
        self.kms = kms_client or boto3.client('kms')
        self.sdk = sdk or aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT, max_encrypted_data_keys=1)
        self.providers = providers or AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())

    def _context(self, session_id: str, surface: str) -> dict:
        if not isinstance(session_id, str) or not 16 <= len(session_id) <= 256 or surface not in {'customer', 'admin'}:
            raise ValueError('invalid session context')
        return {'purpose': 'COGNITO_REFRESH_TOKEN', 'environment': self.environment, 'schema_version': '1',
                'session_hash': hashlib.sha256(session_id.encode('utf-8')).hexdigest(), 'surface': surface}

    def _keyring(self):
        return self.providers.create_aws_kms_keyring(input=CreateAwsKmsKeyringInput(
            kms_key_id=self.key_arn, kms_client=self.kms))

    def seal(self, token: bytearray, session_id: str, surface: str) -> bytes:
        if not isinstance(token, bytearray):
            raise ValueError('mutable token required')
        try:
            context = self._context(session_id, surface)
            if not 1 <= len(token) <= MAX_TOKEN_BYTES:
                raise ValueError('invalid refresh token length')
            ciphertext, header = self.sdk.encrypt(source=bytes(token), keyring=self._keyring(),
                                                   encryption_context=context, algorithm=SUITE)
            if header.algorithm != SUITE or not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
                raise ValueError('invalid encrypted token')
            return ciphertext
        except Exception:
            raise ValueError('unable to encrypt session token') from None
        finally:
            token[:] = b'\x00' * len(token)

    def open(self, ciphertext: bytes, session_id: str, surface: str) -> bytearray:
        context = self._context(session_id, surface)
        if not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
            raise ValueError('invalid encrypted token')
        try:
            plaintext, header = self.sdk.decrypt(source=ciphertext, keyring=self._keyring(), encryption_context=context)
            if header.algorithm != SUITE or any(header.encryption_context.get(k) != v for k, v in context.items()) or \
                    not 1 <= len(plaintext) <= MAX_TOKEN_BYTES:
                raise ValueError('invalid encrypted token')
            return bytearray(plaintext)
        except Exception:
            raise ValueError('unable to decrypt session token') from None
