"""Certificate-pinned OPC UA Binary subscriber for the synthetic vendor-a Mock."""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from cryptography import x509
from jsonschema.exceptions import ValidationError

from foodtech_contracts import decode_record, stream_key
from .aws_flow import DynamoOrderedIngestor
from .ingestion_quarantine import IngestionQuarantine

NAMESPACE = "urn:foodtech:synthetic:opcua:203:a:v1"
SERVER_URI = "urn:localhost:foodtech:synthetic-ua"
INSTANCES = ("mockprofile-203-a-001", "mockprofile-203-b-001", "mockprofile-203-c-001")


def validate_server_certificate(endpoint: str, certificate: str) -> None:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "opc.tcp" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid OPC UA endpoint")
    cert = x509.load_der_x509_certificate(Path(certificate).read_bytes())
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
        raise ValueError("server certificate is outside its validity period")
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if SERVER_URI not in names.get_values_for_type(x509.UniformResourceIdentifier):
        raise ValueError("server application URI does not match the pinned certificate")
    host = parsed.hostname.lower()
    dns = {name.lower() for name in names.get_values_for_type(x509.DNSName)}
    ips = {str(value) for value in names.get_values_for_type(x509.IPAddress)}
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if host not in dns and host not in ips and not (local and socket.gethostname().lower() in dns):
        raise ValueError("server certificate hostname does not match endpoint")


def validate_history(value: str, device_id: str, seen: dict[int, str], batch: str | None,
                     *, source_id: str = "synthetic-opcua"):
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2_000_000:
        raise ValueError("invalid synthetic OPC UA recovery buffer")
    records = json.loads(value)
    if not isinstance(records, list) or not 1 <= len(records) <= 5000:
        raise ValueError("invalid synthetic OPC UA recovery buffer")
    if seen and len(records) < max(seen):
        raise ValueError("synthetic source history moved backwards")
    parsed = []
    for number, raw in enumerate(records, 1):
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 2048:
            raise ValueError("invalid synthetic OPC UA record")
        record = decode_record(raw)
        if (record["sourceId"] != source_id or record["deviceId"] != device_id or
                record["sequenceNumber"] != number):
            raise ValueError("unexpected synthetic OPC UA source or sequence")
        if batch is None:
            batch = record["batchId"]
        if record["batchId"] != batch:
            raise ValueError("OPC UA recovery buffer changed batch")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if number in seen and seen[number] != digest:
            raise ValueError("OPC UA source rewrote a previously seen record")
        parsed.append((number, raw, record, digest))
    return parsed, batch


def validate_or_quarantine(value: str, device_id: str, seen: dict[int, str], batch: str | None,
                           *, source_id: str, profile_id: str, quarantine):
    try:
        return validate_history(value, device_id, seen, batch, source_id=source_id)
    except (ValueError, ValidationError):
        if quarantine is not None and isinstance(value, str):
            quarantine.preserve_opcua_history(value.encode("utf-8"), instance_id=device_id, profile_id=profile_id)
        raise


async def collect(endpoint: str, client_certificate: str, client_key: str, server_certificate: str,
                  ingestor, *, count: int, timeout_seconds: int, instance: str = INSTANCES[0], quarantine=None) -> int:
    if instance not in INSTANCES or not 1 <= count <= 5000 or timeout_seconds < 1:
        raise ValueError("invalid collection limits")
    validate_server_certificate(endpoint, server_certificate)
    client = Client(endpoint, auto_reconnect=True, reconnect_max_delay=2, reconnect_request_timeout=3)
    client.application_uri = "urn:foodtech:opcua:collector"
    await client.set_security(SecurityPolicyBasic256Sha256, certificate=client_certificate, private_key=client_key,
                              server_certificate=server_certificate, mode=ua.MessageSecurityMode.SignAndEncrypt)
    changed = asyncio.Event()
    invalid = asyncio.Event()

    class Handler:
        def datachange_notification(self, _node, value, data):
            status = data.monitored_item.Value.StatusCode
            if status.value == ua.StatusCodes.BadWaitingForInitialData:
                return
            if status.is_bad():
                invalid.set()
            elif isinstance(value, str) and value:
                changed.set()

    async with client:
        vendor = instance.split("-")[2]
        index = await client.get_namespace_index(f"urn:foodtech:synthetic:opcua:203:{vendor}:v1")
        device = await client.nodes.objects.get_child([f"{index}:SyntheticDevice"])
        snapshot = await device.get_child([f"{index}:RecordSnapshot"])
        history = await device.get_child([f"{index}:AvailableSnapshots"])
        device_id = await (await device.get_child([f"{index}:DeviceId"])).read_value()
        if device_id != instance:
            raise ValueError("unexpected synthetic OPC UA device")
        subscription = await client.create_subscription(500, Handler())
        try:
            await subscription.subscribe_data_change(snapshot, queuesize=128, sampling_interval=250)
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            accepted = 0
            streams: set[str] = set()
            seen: dict[int, str] = {}
            expected_batch: str | None = None
            while accepted < count:
                if invalid.is_set():
                    raise RuntimeError("OPC UA notification reported invalid data")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("OPC UA collection deadline reached")
                try:
                    await asyncio.wait_for(changed.wait(), timeout=min(1, remaining))
                except TimeoutError:
                    pass
                changed.clear()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("OPC UA collection deadline reached")
                try:
                    values = await asyncio.wait_for(history.read_value(), timeout=min(4, remaining))
                except ua.UaStatusCodeError as exc:
                    if exc.code not in (ua.StatusCodes.BadSessionIdInvalid, ua.StatusCodes.BadSessionClosed,
                                        ua.StatusCodes.BadSecureChannelClosed, ua.StatusCodes.BadConnectionClosed,
                                        ua.StatusCodes.BadCommunicationError, ua.StatusCodes.BadServerHalted,
                                        ua.StatusCodes.BadWaitingForInitialData):
                        raise
                    await asyncio.sleep(min(.25, max(0, deadline - asyncio.get_running_loop().time())))
                    continue
                except (ConnectionError, OSError, TimeoutError):
                    await asyncio.sleep(min(.25, max(0, deadline - asyncio.get_running_loop().time())))
                    continue
                parsed, expected_batch = await asyncio.to_thread(validate_or_quarantine, values, device_id, seen,
                    expected_batch, source_id="synthetic-opcua", profile_id="mockprofile-203", quarantine=quarantine)
                for number, raw, record, digest in parsed:
                    if number not in seen:
                        await asyncio.to_thread(ingestor.ingest, raw)
                        seen[number] = digest
                        streams.add(stream_key(record))
                        accepted += 1
                        if accepted == count:
                            break
                for stream in streams:
                    await asyncio.to_thread(ingestor.drain, stream)
            if invalid.is_set():
                raise RuntimeError("OPC UA notification reported invalid data")
            return accepted
        finally:
            await subscription.delete()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect a synthetic OPC UA Binary source")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--instance", choices=INSTANCES, default=INSTANCES[0])
    args = parser.parse_args()
    ingestor = DynamoOrderedIngestor(os.environ["FOODTECH_DATA_TABLE"], os.environ["FOODTECH_ORDERED_QUEUE_URL"])
    bucket = os.getenv("FOODTECH_INGESTION_DLQ_BUCKET")
    quarantine = IngestionQuarantine(bucket, os.environ["FOODTECH_DATA_TABLE"]) if bucket else None
    accepted = asyncio.run(collect(os.environ["FOODTECH_OPCUA_ENDPOINT"], os.environ["FOODTECH_OPCUA_CLIENT_CERT"],
        os.environ["FOODTECH_OPCUA_CLIENT_KEY"], os.environ["FOODTECH_OPCUA_SERVER_CERT"], ingestor,
        count=args.count, timeout_seconds=args.timeout, instance=args.instance, quarantine=quarantine))
    print(f"accepted={accepted} provenance=SYNTHETIC")


if __name__ == "__main__":
    main()
