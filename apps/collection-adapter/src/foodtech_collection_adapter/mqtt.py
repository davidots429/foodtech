"""Read-only MQTT 5/TLS subscriber for the synthetic vendor-a Mock."""

import argparse
import hashlib
import os
import ssl
import time

import jsonschema
from lxml import etree
import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from foodtech_contracts import decode_record, stream_key
from .aws_flow import DynamoOrderedIngestor
from .ingestion_quarantine import IngestionQuarantine, failure as ingestion_failure

INSTANCES = tuple(f"mockprofile-{profile}-{vendor}-001" for profile in ("300", "301", "302", "310", "312", "320", "322") for vendor in "abc")
TOPIC = "foodtech/synthetic/mockprofile-300-a-001/observations"


def topic_for(instance: str) -> str:
    if instance not in INSTANCES:
        raise ValueError("unsupported synthetic MQTT instance")
    return f"foodtech/synthetic/{instance}/observations"


def accept_message(client, message, ingestor, quarantine, client_id, instance=INSTANCES[0]):
    """Acknowledge only after a normal event or a quarantined raw payload is durable."""
    if message.topic != topic_for(instance) or message.qos != 1 or message.retain:
        raise ValueError("unexpected MQTT message contract")
    record = None
    xml = instance.startswith("mockprofile-301-")
    csv_source = instance.startswith(("mockprofile-302-", "mockprofile-312-", "mockprofile-322-"))
    try:
        raw = message.payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raw = None
        invalid = ("UNSUPPORTED_ENCODING", [])
    else:
        if xml or csv_source:
            try:
                record = decode_record(raw)
                invalid = None
            except (ValueError, etree.Error, jsonschema.ValidationError):
                invalid = ("SCHEMA_VIOLATION", [])
        else:
            invalid = ingestion_failure(raw)
            if invalid is None:
                try:
                    record = decode_record(raw)
                except (ValueError, etree.Error, jsonschema.ValidationError):
                    invalid = ("SCHEMA_VIOLATION", [])
    if invalid is not None:
        if quarantine is None:
            raise ValueError("ingestion quarantine bucket is required for malformed input")
        quarantine.preserve_mqtt(bytes(message.payload), topic=message.topic, client_id=client_id,
                                 reason=invalid[0], missing=invalid[1],
                                 profile_id="-".join(instance.split("-")[:2]))
        stream = None
    else:
        record = record if record is not None else decode_record(raw)
        if record["sourceId"] != ("synthetic-mqtt-csv" if csv_source else "synthetic-mqtt-xml" if xml else "synthetic-mqtt") or record["deviceId"] != instance:
            raise ValueError("unexpected synthetic source")
        ingestor.ingest(raw)
        stream = stream_key(record)
    if client.ack(message.mid, message.qos) != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError("MQTT acknowledgement failed")
    return stream


def collect(host: str, port: int, ca_file: str, username: str, password: str,
            client_id: str, ingestor, *, count: int, timeout_seconds: int, quarantine=None, instance: str = INSTANCES[0]) -> int:
    if (not host or not 1024 <= port <= 65535 or not ca_file or not username or not password or
            not client_id.startswith("foodtech-collector-") or not 1 <= count <= 5000 or timeout_seconds < 1):
        raise ValueError("invalid MQTT subscriber configuration")
    topic = topic_for(instance)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                         protocol=mqtt.MQTTv5, manual_ack=True)
    client.username_pw_set(username, password)
    client.tls_set_context(ssl.create_default_context(cafile=ca_file))
    received, streams, failure = [], set(), []
    accepted_deliveries = set()
    connected_once = False

    def on_connect(_client, _userdata, _flags, reason, _properties):
        nonlocal connected_once
        if reason.is_failure:
            failure.append("MQTT authentication failed")
        elif connected_once and not _flags.session_present:
            failure.append("MQTT persistent session lost; collection completeness cannot be verified")
        else:
            connected_once = True
            _client.subscribe(topic, qos=1)

    def on_subscribe(_client, _userdata, _mid, reasons, _properties):
        if not reasons or any(reason.is_failure for reason in reasons):
            failure.append("MQTT subscription rejected")

    def on_message(_client, _userdata, message):
        try:
            stream = accept_message(_client, message, ingestor, quarantine, client_id, instance)
            if stream is not None:
                streams.add(stream)
            delivery = (message.mid, hashlib.sha256(message.payload).digest())
            retransmission = bool(message.dup and delivery in accepted_deliveries)
            accepted_deliveries.add(delivery)
            if not retransmission:
                received.append(message.mid)
        except Exception as exc:
            failure.append(f"MQTT message was not durably accepted ({type(exc).__name__})")

    client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
    props = Properties(PacketTypes.CONNECT)
    props.SessionExpiryInterval = 300
    client.connect(host, port, keepalive=30, clean_start=False, properties=props)
    deadline = time.monotonic() + timeout_seconds
    next_drain = time.monotonic() + 1
    next_reconnect = 0.0
    disconnected = False
    try:
        while len(received) < count and not failure and time.monotonic() < deadline:
            if time.monotonic() >= next_drain:
                for stream in streams:
                    ingestor.drain(stream)
                next_drain = time.monotonic() + 1
            if disconnected:
                if time.monotonic() < next_reconnect:
                    time.sleep(max(0, min(0.2, next_reconnect - time.monotonic())))
                    continue
                try:
                    result = client.reconnect()
                except OSError:
                    next_reconnect = time.monotonic() + 1
                    continue
                if result != mqtt.MQTT_ERR_SUCCESS:
                    next_reconnect = time.monotonic() + 1
                    continue
                disconnected = False
            result = client.loop(timeout=0.5)
            if result != mqtt.MQTT_ERR_SUCCESS:
                if result in (mqtt.MQTT_ERR_CONN_LOST, mqtt.MQTT_ERR_NO_CONN):
                    disconnected = True
                    next_reconnect = time.monotonic() + 1
                else:
                    failure.append("MQTT connection failed")
        if failure:
            raise RuntimeError(failure[0])
        if len(received) != count:
            raise TimeoutError("MQTT collection deadline reached")
        return len(received)
    finally:
        client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect the synthetic MQTT Mock over TLS")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--instance", choices=INSTANCES, default=INSTANCES[0])
    args = parser.parse_args()
    ingestor = DynamoOrderedIngestor(os.environ["FOODTECH_DATA_TABLE"], os.environ["FOODTECH_ORDERED_QUEUE_URL"])
    bucket = os.environ.get("FOODTECH_INGESTION_DLQ_BUCKET")
    quarantine = IngestionQuarantine(bucket, os.environ["FOODTECH_DATA_TABLE"]) if bucket else None
    count = collect(os.environ["FOODTECH_MQTT_HOST"], int(os.environ["FOODTECH_MQTT_PORT"]),
                    os.environ["FOODTECH_MQTT_CA_FILE"], os.environ["FOODTECH_MQTT_USER"],
                    os.environ["FOODTECH_MQTT_PASSWORD"], os.environ["FOODTECH_MQTT_CLIENT_ID"], ingestor,
                    count=args.count, timeout_seconds=args.timeout, quarantine=quarantine, instance=args.instance)
    print(f"accepted={count} provenance=SYNTHETIC")


if __name__ == "__main__":
    main()
