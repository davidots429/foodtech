"""Encrypt internal legacy-scan checkpoints; not an authorizer or checkpoint store.

Checkpoints can contain another user's table key. Use only in a restricted
maintenance worker, with a separately approved key and service role. SDK and
Python copies cannot be guaranteed erased from memory.
"""
import json
import re
from uuid import UUID

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateAwsKmsKeyringInput
from aws_encryption_sdk import CommitmentPolicy
from aws_encryption_sdk.identifiers import Algorithm

SUITE = Algorithm.AES_256_GCM_HKDF_SHA512_COMMIT_KEY_ECDSA_P384


def _validate_checkpoint(checkpoint):
    if not isinstance(checkpoint, dict) or set(checkpoint) != {'pk', 'sk'} or any(
            not isinstance(value, str) or not value or len(value) > 2048 for value in checkpoint.values()):
        raise ValueError('invalid checkpoint')


def _validate_subject(subject):
    if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', subject):
        raise ValueError('invalid checkpoint scope')


class ProfileCheckpointCipher:
    def __init__(self, key_arn: str, environment: str, *, kms_client=None, sdk=None, providers=None):
        if not isinstance(key_arn, str) or not re.fullmatch(
                r'arn:aws:kms:ap-southeast-2:[0-9]{12}:key/[A-Za-z0-9-]+', key_arn) or \
                not isinstance(environment, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', environment):
            raise ValueError('invalid checkpoint key configuration')
        self.key_arn, self.environment = key_arn, environment
        self.kms = kms_client or boto3.client('kms', region_name='ap-southeast-2')
        self.sdk = sdk or aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT, max_encrypted_data_keys=1)
        self.providers = providers or AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())

    def _context(self, job_id, phase, version, source_table):
        try:
            if str(UUID(job_id)) != job_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid checkpoint job') from None
        if not isinstance(source_table, str) or not re.fullmatch(r'arn:aws:dynamodb:ap-southeast-2:[0-9]{12}:table/[A-Za-z0-9_.-]{3,255}', source_table):
            raise ValueError('invalid source table')
        if phase not in {'INITIAL', 'CONTINUE', 'SCAN_COMPLETE'} or type(version) is not int or \
                not 1 <= version < 2**63:
            raise ValueError('invalid checkpoint version')
        return {'purpose': 'PROFILE_MIGRATION_CHECKPOINT', 'schema_version': '1',
                'environment': self.environment, 'job_id': job_id, 'phase': phase, 'version': str(version),
                'source_table': source_table}

    def _keyring(self):
        return self.providers.create_aws_kms_keyring(input=CreateAwsKmsKeyringInput(
            kms_key_id=self.key_arn, kms_client=self.kms))

    def seal(self, job_id: str, subject: str, checkpoint: dict, *, phase: str, version: int, source_table: str) -> bytes:
        context = self._context(job_id, phase, version, source_table)
        _validate_subject(subject)
        _validate_checkpoint(checkpoint)
        raw = bytearray(json.dumps({'subject': subject, 'checkpoint': checkpoint}, ensure_ascii=True,
                                   separators=(',', ':'), sort_keys=True).encode('ascii'))
        try:
            ciphertext, header = self.sdk.encrypt(source=bytes(raw), keyring=self._keyring(),
                                                  encryption_context=context, algorithm=SUITE)
            if header.algorithm != SUITE or not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= 65536:
                raise ValueError('invalid checkpoint ciphertext')
            return ciphertext
        finally:
            raw[:] = b'\x00' * len(raw)

    def open(self, job_id: str, subject: str, ciphertext: bytes, *, phase: str, version: int, source_table: str) -> dict:
        context = self._context(job_id, phase, version, source_table)
        _validate_subject(subject)
        if not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= 65536:
            raise ValueError('invalid checkpoint ciphertext')
        raw = bytearray()
        try:
            plaintext, header = self.sdk.decrypt(source=ciphertext, keyring=self._keyring(), encryption_context=context)
            actual = header.encryption_context
            if header.algorithm != SUITE or not isinstance(actual, dict) or \
                    not set(actual) <= set(context) | {'aws-crypto-public-key'} or \
                    any(actual.get(k) != v for k, v in context.items()) or len(plaintext) > 32768:
                raise ValueError('invalid checkpoint context')
            raw.extend(plaintext)
            result = json.loads(raw)
            if not isinstance(result, dict) or set(result) != {'subject', 'checkpoint'} or result['subject'] != subject:
                raise ValueError('invalid checkpoint scope')
            _validate_checkpoint(result['checkpoint'])
            return result['checkpoint']
        except Exception:
            raise ValueError('unable to decrypt checkpoint') from None
        finally:
            raw[:] = b'\x00' * len(raw)
