"""Restricted decrypt boundary for deletion-registry recovery.

Use only from an authenticated, separately authorized recovery worker. This
component does not authenticate callers, validate customer ownership, or
supply IAM/JIT grants. Python and SDK internal copies cannot be guaranteed erased.
"""
import re
from uuid import UUID

import aws_encryption_sdk
import boto3
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateAwsKmsKeyringInput
from aws_encryption_sdk import CommitmentPolicy

from .deletion_registry_cipher import SUITE


class DeletionRegistryRecoveryCipher:
    def __init__(self, key_arn: str, environment: str, *, kms_client=None, sdk=None, providers=None):
        if not isinstance(key_arn, str) or not re.fullmatch(
                r'arn:aws:kms:ap-southeast-2:[0-9]{12}:key/[A-Za-z0-9-]+', key_arn) or \
                not isinstance(environment, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', environment):
            raise ValueError('invalid registry recovery key configuration')
        self.key_arn, self.environment = key_arn, environment
        self.kms = kms_client or boto3.client('kms', region_name='ap-southeast-2')
        self.sdk = sdk or aws_encryption_sdk.EncryptionSDKClient(
            commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT, max_encrypted_data_keys=1)
        self.providers = providers or AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())

    def use(self, *, request_id: str, encrypted_user_id: bytes, encrypted_capsule_locator: bytes, callback):
        """Use both mutable plaintext buffers only inside callback; erase them on exit."""
        user, locator = bytearray(), bytearray()
        try:
            if str(UUID(request_id)) != request_id or not callable(callback) or any(
                    not isinstance(value, bytes) or not 1 <= len(value) <= 65536
                    for value in (encrypted_user_id, encrypted_capsule_locator)):
                raise ValueError
            keyring = self.providers.create_aws_kms_keyring(input=CreateAwsKmsKeyringInput(
                kms_key_id=self.key_arn, kms_client=self.kms))
            for field, ciphertext, target in (('internal_user_id', encrypted_user_id, user),
                                               ('capsule_locator', encrypted_capsule_locator, locator)):
                context = {'purpose': 'DELETION_REGISTRY', 'environment': self.environment,
                           'schema_version': '1', 'request_id': request_id, 'field': field}
                plaintext, header = self.sdk.decrypt(source=ciphertext, keyring=keyring, encryption_context=context)
                actual = header.encryption_context
                if header.algorithm != SUITE or not isinstance(actual, dict) or \
                        not set(actual) <= set(context) | {'aws-crypto-public-key'} or \
                        any(actual.get(key) != value for key, value in context.items()) or \
                        not isinstance(plaintext, bytes) or not 1 <= len(plaintext) <= 4096:
                    raise ValueError
                target.extend(plaintext)
            user_text, locator_text = user.decode('ascii'), locator.decode('ascii')
            if str(UUID(user_text)) != user_text or not re.fullmatch(r'[A-Za-z0-9_-]{43}', locator_text):
                raise ValueError
            return callback(user, locator)
        except Exception:
            raise ValueError('unable to recover deletion registry locators') from None
        finally:
            user[:] = b'\x00' * len(user)
            locator[:] = b'\x00' * len(locator)
