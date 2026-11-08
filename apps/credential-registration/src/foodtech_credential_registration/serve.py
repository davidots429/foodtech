"""Dedicated loopback mTLS listener for the Business API registration client."""

import os
import ssl

import uvicorn

from .main import app


def main() -> None:
    cert = os.environ["FOODTECH_REGISTRATION_SERVER_CERT"]
    key = os.environ["FOODTECH_REGISTRATION_SERVER_KEY"]
    business_ca = os.environ["FOODTECH_REGISTRATION_BUSINESS_CA"]
    if not all(os.path.isfile(path) for path in (cert, key, business_ca)):
        raise ValueError("registration mTLS certificate files are required")
    app.state.mtls_listener = True
    uvicorn.run(app, host=os.getenv("FOODTECH_REGISTRATION_BIND_HOST", "127.0.0.1"),
                port=int(os.getenv("FOODTECH_REGISTRATION_PORT", "8444")),
                ssl_certfile=cert, ssl_keyfile=key, ssl_ca_certs=business_ca,
                ssl_cert_reqs=ssl.CERT_REQUIRED, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
