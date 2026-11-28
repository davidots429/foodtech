"""Verify pinned Tonkatsu peer transport with a read-only unrouted request."""

import hashlib
from http.client import HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import signal
import ssl
import sys

PREFIX = "FOODTECH_TONKATSU_"
DOMAIN = "tonkatsu.foodtech.internal"
PROBE_PATH = "/internal/tonkatsu/transport-probe"

LANES = {
    "tonkatsu-business-api": (
        ("broker", 8446, "BROKER_URL", "BROKER_SERVER_CERT", "BFF_CERT", "BFF_KEY"),
        ("records", 8447, "GATEWAY_RECORDS_URL", "GATEWAY_RECORDS_SERVER_CERT",
         "GATEWAY_RECORDS_CLIENT_CERT", "GATEWAY_RECORDS_CLIENT_KEY"),
    ),
    "tonkatsu-broker": (
        ("gateway", 8445, "GATEWAY_URL", "GATEWAY_SERVER_CERT", "BROKER_CLIENT_CERT", "BROKER_CLIENT_KEY"),
    ),
    "tonkatsu-gateway": (
        ("inference", 8448, "OLLAMA_PRIVATE_ORIGIN", "OLLAMA_SERVER_CERT", "OLLAMA_CLIENT_CERT", "OLLAMA_CLIENT_KEY"),
    ),
}


class ProbeFailed(ValueError):
    """A bounded transport test failed."""


def pinned_context(server_leaf, client_certificate, client_key):
    with Path(server_leaf).open("rb") as source:
        certificate = source.read(65537)
    start = b"-----BEGIN CERTIFICATE-----"
    end = b"-----END CERTIFICATE-----"
    if (len(certificate) > 65536 or certificate.count(start) != 1 or certificate.count(end) != 1
            or not certificate.strip().startswith(start) or not certificate.strip().endswith(end)):
        raise ProbeFailed("invalid server leaf")
    pem = certificate.decode("ascii").strip()
    pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=pem)
    context.load_cert_chain(client_certificate, client_key)
    return context, pin


def probe_peer(host, port, context, pin):
    connection = HTTPSConnection(host, port, context=context, timeout=5)
    try:
        connection.connect()
        peer = connection.sock.getpeercert(binary_form=True)
        if not peer or hashlib.sha256(peer).digest() != pin:
            raise ProbeFailed("unexpected server leaf")
        connection.request("GET", PROBE_PATH, headers={"Connection": "close"})
        with connection.getresponse() as response:
            # The native mTLS protocol closes wrong leaves before dispatch. A
            # 404 is expected for this path, which has no business handler.
            if response.status != 404:
                raise ProbeFailed("unexpected response on unrouted path")
    finally:
        connection.close()


def probe(environ, completed):
    if any(environ.get(key) for key in ("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI")):
        raise ProbeFailed("transport probe requires a task without workload credentials")
    for name, port, origin_key, server_key, cert_key, private_key in LANES[environ["FOODTECH_SERVICE"]]:
        host = f"{name}.{DOMAIN}"
        if environ.get(PREFIX + origin_key) != f"https://{host}:{port}":
            raise ProbeFailed("unexpected private origin")
        context, pin = pinned_context(*(environ[PREFIX + key] for key in (server_key, cert_key, private_key)))
        probe_peer(host, port, context, pin)
        completed.append(name)


def expired(_signum, _frame):
    raise TimeoutError("transport probe timed out")


def main():
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(30)
    completed = []
    try:
        probe(os.environ, completed)
    except (KeyError, ValueError, OSError, HTTPException) as error:
        print(json.dumps({"status": "not_verified", "lanes": completed, "error_type": type(error).__name__}),
              file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
    print(json.dumps({"status": "verified_private_transport", "lanes": completed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
