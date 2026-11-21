"""Opt-in synthetic OPC UA JSON -> DynamoDB -> FIFO -> Worker live check.

Uses existing development resources; unique batches and their records are removed.
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import boto3
import httpx
from foodtech_collection_adapter.aws_flow import DynamoOrderedIngestor
from foodtech_collection_adapter.opcua_json import collect
from foodtech_contracts import decode_record, dedup_key, stream_binding_key
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker
from mock_quality_dynamodb_check import empty_queue, event_ids, remove_quarantine_messages
from process_flow_dynamodb_check import (ACCOUNT, DATA_TABLE, ORDERED_QUEUE, QUARANTINE_QUEUE,
                                         REGION, remove_partition, remove_test_messages)

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / 'apps/mock-control/bin/Release/net10.0/MockControl.dll'


def check_one(sqs, table, ordered_url, quarantine_url, profile, vendor):
    instance = f'mockprofile-{profile}-{vendor}-001'
    fixture = json.loads((ROOT / f'fixtures/{instance}.json').read_text())
    unique = uuid.uuid4().hex
    batch = f'batch-{vendor}-live-{unique}'
    batch_key, record_key = {'a': ('batchId', 'recordId'), 'b': ('batch_id', 'record_id'),
                             'c': ('BatchId', 'RecId')}[vendor]
    for index, record in enumerate(fixture['records'], 1):
        record[batch_key] = batch
        record[record_key] = f'rec-{vendor}-live-{unique}-{index}'
    stream = stream_binding_key('synthetic-opcua-json', instance, batch)
    keys = [dedup_key(decode_record(json.dumps(record))) for record in fixture['records']]
    ingestor = DynamoOrderedIngestor(DATA_TABLE, ordered_url)
    worker = DynamoStandardizationWorker(DATA_TABLE, ordered_url, quarantine_url)
    try:
        with tempfile.TemporaryDirectory(prefix='foodtech-ua-live-') as directory:
            temp = Path(directory)
            fixture_path, key, cert, pfx = (temp / x for x in ('fixture.json', 'key.pem', 'cert.pem', 'cert.pfx'))
            fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
            password, secret = uuid.uuid4().hex, uuid.uuid4().hex
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                            '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                            '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['openssl', 'pkcs12', '-export', '-out', str(pfx), '-inkey', str(key), '-in', str(cert),
                            '-password', f'pass:{password}'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            base = f'https://localhost:{port}'
            server = subprocess.Popen(['dotnet', str(DLL), 'serve-ua-json', '--fixture', str(fixture_path),
                                       '--cert', str(pfx), '--port', str(port)],
                                      env={**os.environ, 'FOODTECH_MOCK_CLIENT_ID': 'synthetic-test',
                                           'FOODTECH_MOCK_CLIENT_SECRET': secret, 'FOODTECH_MOCK_CERT_PASSWORD': password},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError('Mock exited before startup')
                    try:
                        if httpx.get(base + '/health', verify=str(cert), timeout=.3).status_code == 200:
                            break
                    except httpx.TransportError:
                        time.sleep(.1)
                else:
                    raise TimeoutError('Mock did not start')
                if collect(base, 'synthetic-test', secret, str(cert), ingestor, count=3,
                           timeout_seconds=20, instance=instance) != 3:
                    raise AssertionError('Unexpected collection count')
            finally:
                server.terminate()
                server.wait(timeout=5)
        processed = []
        for _ in range(8):
            for message in sqs.receive_message(QueueUrl=ordered_url, MaxNumberOfMessages=10,
                    WaitTimeSeconds=1, VisibilityTimeout=30).get('Messages', []):
                ref = json.loads(message['Body'])
                if ref.get('pk') != stream:
                    sqs.change_message_visibility(QueueUrl=ordered_url, ReceiptHandle=message['ReceiptHandle'], VisibilityTimeout=0)
                    raise RuntimeError('Unexpected message in shared queue')
                raw_item = table.get_item(Key={'pk': stream, 'sk': ref['raw_sk']}, ConsistentRead=True)['Item']
                result = worker.process(ref)
                assessment = table.get_item(Key={'pk': f"EVENT#{result['ingestion_event_id']}",
                                                  'sk': 'ASSESSMENT#v1'}, ConsistentRead=True)['Item']
                processed.append((int(raw_item['sequence_number']), result, assessment))
                sqs.delete_message(QueueUrl=ordered_url, ReceiptHandle=message['ReceiptHandle'])
            if len(processed) == 3:
                break
        if [x[0] for x in processed] != [1, 2, 3] or [x[1]['quality_status'] for x in processed] != ['VALID', 'VALID', 'QUARANTINED']:
            raise AssertionError('Unexpected FIFO order or quality results')
        for _, outcome, assessment in processed:
            if outcome['dedup_status'] != 'UNIQUE' or assessment['mapping_version'] != f'mockprofile-{profile}-{vendor}-map-v1':
                raise AssertionError('Unexpected deduplication or mapping')
        data = processed[0][2]['normalized']
        if profile == '210' and ('POT_TEMPERATURE' not in data or 'CHAMBER_TEMPERATURE' in data):
            raise AssertionError('Stirring temperature was not preserved')
        if profile == '220' and (str(data['CLIMATE_SETTING_PERCENT']['value']) != '100' or
                                 str(data['RELATIVE_HUMIDITY']['value']) != '98' or
                                 int(data['FAN_SPEED_LEVEL']['value']) != 4):
            raise AssertionError('Combi climate, measured humidity or fan was not preserved')
        event = processed[-1][1]['ingestion_event_id']
        messages = sqs.receive_message(QueueUrl=quarantine_url, MaxNumberOfMessages=10,
                                       WaitTimeSeconds=2, VisibilityTimeout=30).get('Messages', [])
        if len(messages) != 1 or json.loads(messages[0]['Body']).get('ingestion_event_id') != event:
            for message in messages:
                sqs.change_message_visibility(QueueUrl=quarantine_url, ReceiptHandle=message['ReceiptHandle'], VisibilityTimeout=0)
            raise AssertionError('Unexpected quarantine queue contents')
        sqs.delete_message(QueueUrl=quarantine_url, ReceiptHandle=messages[0]['ReceiptHandle'])
        print(f'{instance}: collected 3, ordered 1/2/3, valid 2, quarantined 1', flush=True)
    finally:
        ids = event_ids(table, stream)
        remove_test_messages(sqs, ordered_url, stream)
        remove_quarantine_messages(sqs, quarantine_url, ids)
        for key in keys:
            digest = hashlib.sha256(key.encode()).hexdigest()
            item_key = {'pk': f'DEDUP#{digest}', 'sk': 'CANONICAL'}
            table.delete_item(Key=item_key)
            if table.get_item(Key=item_key, ConsistentRead=True).get('Item'):
                raise AssertionError('Synthetic deduplication item remains')
        for event in ids:
            remove_partition(table, f'EVENT#{event}')
        remove_partition(table, stream)
        print(f'{instance}: synthetic cleanup complete', flush=True)


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or not shutil.which('dotnet') or not DLL.is_file():
        raise SystemExit('AWS_PROFILE=foodtech and built .NET 10 Mock are required')
    session = boto3.Session(profile_name='foodtech', region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise SystemExit('Unexpected AWS account')
    sqs = session.client('sqs')
    ordered = sqs.get_queue_url(QueueName=ORDERED_QUEUE)['QueueUrl']
    quarantine = sqs.get_queue_url(QueueName=QUARANTINE_QUEUE)['QueueUrl']
    table = session.resource('dynamodb').Table(DATA_TABLE)
    for profile in ('210', '220'):
        for vendor in 'abc':
            if not empty_queue(sqs, ordered) or not empty_queue(sqs, quarantine):
                raise SystemExit('Shared development queues must be empty')
            check_one(sqs, table, ordered, quarantine, profile, vendor)


if __name__ == '__main__':
    main()
