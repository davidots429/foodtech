"""Opt-in synthetic checkpoint encryption test; does not persist checkpoint data."""
import os
import logging
from uuid import uuid4

import boto3
from foodtech_business_api.profile_checkpoint_cipher import ProfileCheckpointCipher


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    kms = session.client('kms')
    key = kms.describe_key(KeyId='alias/foodtech/ai-credential-dev-2026-11')['KeyMetadata']['Arn']
    cipher = ProfileCheckpointCipher(key, 'dev', kms_client=kms)
    job = str(uuid4())
    subject = 'synthetic-owner-' + uuid4().hex
    checkpoint = {'pk': 'synthetic-other-owner-' + uuid4().hex, 'sk': 'META'}
    ciphertext = cipher.seal(job, subject, checkpoint, phase='CONTINUE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:573693340100:table/foodtech-cooking-jobs-dev')
    if checkpoint['pk'].encode() in ciphertext or cipher.open(job, subject, ciphertext, phase='CONTINUE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:573693340100:table/foodtech-cooking-jobs-dev') != checkpoint:
        raise AssertionError('checkpoint encryption round trip failed')
    logger = logging.getLogger('aws_encryption_sdk')
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        for identifier, owner, value in ((str(uuid4()), subject, ciphertext), (job, 'other-owner', ciphertext),
                                         (job, subject, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))):
            try:
                cipher.open(identifier, owner, value, phase='CONTINUE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:573693340100:table/foodtech-cooking-jobs-dev')
            except ValueError:
                continue
            raise AssertionError('wrong job or corrupted ciphertext was accepted')
        try:
            cipher.open(job, subject, ciphertext, phase='CONTINUE', version=3, source_table='arn:aws:dynamodb:ap-southeast-2:573693340100:table/foodtech-cooking-jobs-dev')
        except ValueError:
            pass
        else:
            raise AssertionError('old ciphertext accepted for new version')
        try:
            cipher.open(job, subject, ciphertext, phase='CONTINUE', version=2, source_table='arn:aws:dynamodb:ap-southeast-2:999999999999:table/foodtech-cooking-jobs-dev')
        except ValueError:
            pass
        else:
            raise AssertionError('checkpoint accepted for a different source table')
    finally:
        logger.setLevel(previous)
    print('synthetic checkpoint encryption, job binding and tamper rejection verified')


if __name__ == '__main__':
    main()
