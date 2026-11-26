"""Request-bound encryption for internal deletion progress checkpoints."""
import base64
import json
import re
from uuid import UUID

from .subject_identity import valid_subject

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateAwsKmsKeyringInput
from aws_encryption_sdk import CommitmentPolicy
from aws_encryption_sdk.identifiers import Algorithm

SUITE = Algorithm.AES_256_GCM_HKDF_SHA512_COMMIT_KEY_ECDSA_P384


class DeletionCheckpointCipher:
    def __init__(self, key_arn: str, environment: str, *, source_table_arn: str, kms_client=None, sdk=None, providers=None):
        if not isinstance(key_arn, str) or not re.fullmatch(
                r'arn:aws:kms:ap-southeast-2:[0-9]{12}:key/[A-Za-z0-9-]+', key_arn) or \
                not isinstance(environment, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', environment) or \
                not isinstance(source_table_arn, str) or not re.fullmatch(
                    r'arn:aws:dynamodb:ap-southeast-2:[0-9]{12}:table/[A-Za-z0-9_.-]{3,255}', source_table_arn) or \
                key_arn.split(':')[4] != source_table_arn.split(':')[4]:
            raise ValueError('invalid checkpoint key configuration')
        self.key_arn, self.environment, self.source_table_arn = key_arn, environment, source_table_arn
        self.kms = kms_client or boto3.client('kms', region_name='ap-southeast-2')
        self.sdk = sdk or aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT, max_encrypted_data_keys=1)
        self.providers = providers or AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())

    def _request_context(self, request_id, domain, version, state):
        try:
            if str(UUID(request_id)) != request_id or \
                    not isinstance(domain, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', domain) or \
                    type(version) is not int or not 1 <= version < 2**63 or \
                    state not in {'IN_PROGRESS', 'VERIFYING', 'FAILED'}:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid deletion checkpoint scope') from None
        return {'purpose': 'DELETION_PROGRESS_CHECKPOINT', 'schema_version': '1',
                'environment': self.environment, 'source_table': self.source_table_arn,
                'request_id': request_id, 'domain': domain,
                'version': str(version), 'state': state}

    def _context(self, subject, request_id, domain, version, state):
        if not valid_subject(subject):
            raise ValueError('invalid deletion checkpoint subject')
        return self._request_context(request_id, domain, version, state)

    def _keyring(self):
        return self.providers.create_aws_kms_keyring(input=CreateAwsKmsKeyringInput(
            kms_key_id=self.key_arn, kms_client=self.kms))

    def seal(self, *, subject, request_id, domain, version, state, checkpoint):
        context = self._context(subject, request_id, domain, version, state)
        if not isinstance(checkpoint, bytes) or not 1 <= len(checkpoint) <= 16384:
            raise ValueError('invalid deletion checkpoint')
        raw = bytearray(json.dumps({'subject': subject, 'checkpoint': base64.b64encode(checkpoint).decode('ascii')},
                                   separators=(',', ':'), sort_keys=True).encode('ascii'))
        try:
            ciphertext, header = self.sdk.encrypt(source=bytes(raw), keyring=self._keyring(),
                                                  encryption_context=context, algorithm=SUITE)
            if header.algorithm != SUITE or not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= 65536:
                raise ValueError('invalid deletion checkpoint ciphertext')
            return ciphertext
        finally:
            raw[:] = b'\x00' * len(raw)

    def _use_plaintext(self, *, context, ciphertext, action):
        if not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= 65536:
            raise ValueError('invalid deletion checkpoint ciphertext')
        raw, checkpoint = bytearray(), bytearray()
        try:
            plaintext, header = self.sdk.decrypt(source=ciphertext, keyring=self._keyring(), encryption_context=context)
            actual = header.encryption_context
            if header.algorithm != SUITE or not isinstance(actual, dict) or \
                    not set(actual) <= set(context) | {'aws-crypto-public-key'} or \
                    any(actual.get(key) != value for key, value in context.items()) or \
                    not isinstance(plaintext, bytes) or len(plaintext) > 24000:
                raise ValueError
            raw.extend(plaintext)
            result = json.loads(raw)
            if not isinstance(result, dict) or set(result) != {'subject', 'checkpoint'} or not valid_subject(result['subject']):
                raise ValueError
            checkpoint.extend(base64.b64decode(result['checkpoint'], validate=True))
            if not 1 <= len(checkpoint) <= 16384:
                raise ValueError
            return action(result['subject'], checkpoint)
        except Exception:
            raise ValueError('unable to decrypt deletion checkpoint') from None
        finally:
            raw[:] = b'\x00' * len(raw)
            checkpoint[:] = b'\x00' * len(checkpoint)

    def open(self, *, subject, request_id, domain, version, state, ciphertext):
        context = self._context(subject, request_id, domain, version, state)
        def use(actual_subject, checkpoint):
            if actual_subject != subject:
                raise ValueError('checkpoint subject mismatch')
            return bytes(checkpoint)
        return self._use_plaintext(context=context, ciphertext=ciphertext, action=use)

    def use_source_inventory_head(self, *, request_id, ciphertext, action):
        """Recover the retired subject only inside a trusted cleanup callback.

        Requires a separately authorized recovery worker/key. This neither
        authenticates callers nor exports the subject or callback result.
        Immutable Python/SDK string copies cannot be guaranteed erased.
        """
        if not callable(action) or not re.fullmatch(
                r'arn:aws:dynamodb:ap-southeast-2:[0-9]{12}:table/foodtech-source-access-[a-z0-9-]{1,32}',
                self.source_table_arn):
            raise ValueError('restricted source inventory recovery required')
        context = self._request_context(request_id, 'SOURCE_GRANT_INVENTORY', 1, 'VERIFYING')
        def use(subject, checkpoint):
            action(subject, checkpoint)
        self._use_plaintext(context=context, ciphertext=ciphertext, action=use)
