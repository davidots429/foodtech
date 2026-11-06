"""Dedicated loopback mTLS listener for Business API -> AI Gateway calls."""

import os
import ssl

import uvicorn

from .main import app


def main() -> None:
    cert = os.environ["FOODTECH_GATEWAY_SERVER_CERT"]
    key = os.environ["FOODTECH_GATEWAY_SERVER_KEY"]
    business_ca = os.environ["FOODTECH_GATEWAY_BUSINESS_CA"]
    if not all(os.path.isfile(path) for path in (cert, key, business_ca)):
        raise ValueError("mTLS certificate files are required")
    if not os.getenv("FOODTECH_AI_REGISTRY_TABLE"):
        raise ValueError("AI registry table is required")
    app.state.mtls_listener = True
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("FOODTECH_GATEWAY_PORT", "8443")),
                ssl_certfile=cert, ssl_keyfile=key, ssl_ca_certs=business_ca,
                ssl_cert_reqs=ssl.CERT_REQUIRED, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
