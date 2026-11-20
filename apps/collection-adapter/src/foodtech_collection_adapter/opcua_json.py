"""Project-specific synthetic sessionless Browse/Read over authenticated HTTPS."""
import argparse
import base64
import json
import os
import ssl
import time
from urllib.parse import urlsplit

import httpx
from foodtech_contracts import stream_key
from .aws_flow import DynamoOrderedIngestor
from .opcua import validate_history


INSTANCES = ("mockprofile-200-a-001", "mockprofile-200-b-001", "mockprofile-200-c-001")


class UaJsonClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, ca_file: str):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not ca_file:
            raise ValueError("HTTPS endpoint and trusted CA required")
        self.client = httpx.Client(base_url=base_url, verify=ssl.create_default_context(cafile=ca_file), timeout=5, follow_redirects=False)
        self.authorization = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self.token = None

    def close(self):
        self.client.close()

    def authorize(self):
        response = self.client.post("/oauth2/token", data={"grant_type": "client_credentials"},
                                    headers={"Authorization": self.authorization})
        response.raise_for_status()
        body = response.json()
        if body.get("token_type") != "Bearer" or not isinstance(body.get("access_token"), str) or not body["access_token"]:
            raise ValueError("invalid token response")
        self.token = body["access_token"]

    def get(self, path: str, **params):
        if self.token is None:
            self.authorize()
        for attempt in range(2):
            response = self.client.get(path, params=params, headers={"Authorization": f"Bearer {self.token}"})
            if response.status_code == 401 and attempt == 0:
                self.authorize()
                continue
            response.raise_for_status()
            if (response.headers.get("X-Contract-Version") != "1.0" or
                response.headers.get("X-Profile-ID") != "mockprofile-200" or
                response.headers.get("X-Provenance") != "SYNTHETIC"):
                raise ValueError("unexpected synthetic contract")
            return response.json()
        raise RuntimeError("authentication failed")

    def read(self, node: str):
        response = self.get("/ua-json/v1/read", node=node)
        if response.get("node") != node or "value" not in response:
            raise ValueError("unexpected read response")
        return response["value"]


def collect(base_url: str, client_id: str, client_secret: str, ca_file: str, ingestor, *,
            count: int, timeout_seconds: int, instance: str = INSTANCES[0]) -> int:
    if instance not in INSTANCES or not 1 <= count <= 5000 or timeout_seconds < 1:
        raise ValueError("invalid collection limits")
    client = UaJsonClient(base_url, client_id, client_secret, ca_file)
    try:
        if client.get("/ua-json/v1/browse").get("nodes") != ["DeviceId", "AvailableSnapshots"]:
            raise ValueError("unexpected synthetic nodes")
        device = client.read("DeviceId")
        if device != instance:
            raise ValueError("unexpected synthetic device")
        deadline = time.monotonic() + timeout_seconds
        seen = {}
        batch = None
        streams = set()
        while len(seen) < count:
            if time.monotonic() >= deadline:
                raise TimeoutError("synthetic Browse/Read deadline reached")
            try:
                values = client.read("AvailableSnapshots")
            except httpx.TransportError:
                time.sleep(min(.25, max(0, deadline - time.monotonic())))
                continue
            if not isinstance(values, list) or (seen and len(values) < max(seen)):
                raise ValueError("synthetic source history moved backwards")
            parsed, batch = validate_history(json.dumps(values, ensure_ascii=False), device, seen, batch,
                                              source_id="synthetic-opcua-json")
            for number, raw, record, digest in parsed:
                if number not in seen:
                    ingestor.ingest(raw)
                    seen[number] = digest
                    streams.add(stream_key(record))
                    if len(seen) == count:
                        break
            for stream in streams:
                ingestor.drain(stream)
            if len(seen) < count:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        return len(seen)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description="Collect project-specific synthetic sessionless Browse/Read")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--instance", choices=INSTANCES, default=INSTANCES[0])
    args = parser.parse_args()
    ingestor = DynamoOrderedIngestor(os.environ["FOODTECH_DATA_TABLE"], os.environ["FOODTECH_ORDERED_QUEUE_URL"])
    accepted = collect(os.environ["FOODTECH_UA_JSON_URL"], os.environ["FOODTECH_MOCK_CLIENT_ID"],
                       os.environ["FOODTECH_MOCK_CLIENT_SECRET"], os.environ["FOODTECH_MOCK_CA"], ingestor,
                       count=args.count, timeout_seconds=args.timeout, instance=args.instance)
    print(f"accepted={accepted} provenance=SYNTHETIC")


if __name__ == "__main__":
    main()
