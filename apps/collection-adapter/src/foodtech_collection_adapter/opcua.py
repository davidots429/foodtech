"""Certificate-pinned OPC UA Binary subscriber for the synthetic vendor-a Mock."""

import argparse
import asyncio
import ipaddress
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from cryptography import x509

from foodtech_contracts import decode_record, stream_key
from .aws_flow import DynamoOrderedIngestor

NAMESPACE = "urn:foodtech:synthetic:opcua:203:a:v1"
SERVER_URI = "urn:localhost:foodtech:synthetic-ua"


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


async def collect(endpoint: str, client_certificate: str, client_key: str, server_certificate: str,
                  ingestor, *, count: int, timeout_seconds: int) -> int:
    if not 1 <= count <= 5000 or timeout_seconds < 1:
        raise ValueError("invalid collection limits")
    validate_server_certificate(endpoint, server_certificate)
    client = Client(endpoint)
    client.application_uri = "urn:foodtech:opcua:collector"
    await client.set_security(SecurityPolicyBasic256Sha256, certificate=client_certificate, private_key=client_key,
                              server_certificate=server_certificate, mode=ua.MessageSecurityMode.SignAndEncrypt)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
    overflow = asyncio.Event()

    class Handler:
        def datachange_notification(self, _node, value, data):
            status = data.monitored_item.Value.StatusCode
            if status.value == ua.StatusCodes.BadWaitingForInitialData:
                return
            if status.is_bad():
                overflow.set()
            elif isinstance(value, str) and value:
                try:
                    queue.put_nowait(value)
                except asyncio.QueueFull:
                    overflow.set()

    async with client:
        index = await client.get_namespace_index(NAMESPACE)
        device = await client.nodes.objects.get_child([f"{index}:SyntheticDevice"])
        snapshot = await device.get_child([f"{index}:RecordSnapshot"])
        device_id = await (await device.get_child([f"{index}:DeviceId"])).read_value()
        if device_id != "mockprofile-203-a-001":
            raise ValueError("unexpected synthetic OPC UA device")
        subscription = await client.create_subscription(500, Handler())
        try:
            await subscription.subscribe_data_change(snapshot, queuesize=128, sampling_interval=250)
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            accepted = 0
            streams: set[str] = set()
            while accepted < count:
                if overflow.is_set():
                    raise RuntimeError("OPC UA notification was lost or invalid")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("OPC UA collection deadline reached")
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=min(1, remaining))
                except TimeoutError:
                    for stream in streams:
                        await asyncio.to_thread(ingestor.drain, stream)
                    continue
                record = decode_record(raw)
                if record["sourceId"] != "synthetic-opcua" or record["deviceId"] != device_id:
                    raise ValueError("unexpected synthetic OPC UA source")
                await asyncio.to_thread(ingestor.ingest, raw)
                streams.add(stream_key(record))
                accepted += 1
            if overflow.is_set():
                raise RuntimeError("OPC UA notification was lost or invalid")
            return accepted
        finally:
            await subscription.delete()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect a synthetic OPC UA Binary source")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    ingestor = DynamoOrderedIngestor(os.environ["FOODTECH_DATA_TABLE"], os.environ["FOODTECH_ORDERED_QUEUE_URL"])
    accepted = asyncio.run(collect(os.environ["FOODTECH_OPCUA_ENDPOINT"], os.environ["FOODTECH_OPCUA_CLIENT_CERT"],
        os.environ["FOODTECH_OPCUA_CLIENT_KEY"], os.environ["FOODTECH_OPCUA_SERVER_CERT"], ingestor,
        count=args.count, timeout_seconds=args.timeout))
    print(f"accepted={accepted} provenance=SYNTHETIC")


if __name__ == "__main__":
    main()
