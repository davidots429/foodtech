"""Opt-in Gateway listener for the certificate-pinned fryer broker."""
import os

import uvicorn

from .combi_tls import broker_server_config
from .tonkatsu_read_handoff import TLS_PEER_KEY, TLS_PIN_APP_KEY


BROKER_URI = "urn:foodtech:service:tonkatsu-read-broker"


def tonkatsu_server_config(app, *, server_cert, server_key, broker_ca, broker_certificate,
                           host="127.0.0.1", port=8445):
    return broker_server_config(app, recipe="tonkatsu", broker_uri=BROKER_URI,
        peer_state_key=TLS_PEER_KEY, pin_app_key=TLS_PIN_APP_KEY, server_cert=server_cert,
        server_key=server_key, broker_ca=broker_ca, broker_certificate=broker_certificate,
        host=host, port=port)


def main():
    if os.getenv("FOODTECH_TONKATSU_LISTENER_ENABLED") != "true":
        raise ValueError("fryer listener disabled")
    if not os.getenv("FOODTECH_AI_REGISTRY_TABLE") or not os.getenv("FOODTECH_AUTH_TABLE"):
        raise ValueError("fryer registry and authentication tables required")
    from .main import app

    if "FOODTECH_TONKATSU_OLLAMA_PRIVATE_ORIGIN" in os.environ:
        from .tonkatsu_invocation import configured_tonkatsu_ollama

        app.state.ollama_client = configured_tonkatsu_ollama()
    config = tonkatsu_server_config(app, server_cert=os.environ["FOODTECH_TONKATSU_SERVER_CERT"],
        server_key=os.environ["FOODTECH_TONKATSU_SERVER_KEY"], broker_ca=os.environ["FOODTECH_TONKATSU_BROKER_CA"],
        broker_certificate=os.environ["FOODTECH_TONKATSU_BROKER_CERT"],
        host=os.getenv("FOODTECH_TONKATSU_BIND_HOST", "127.0.0.1"), port=int(os.getenv("FOODTECH_TONKATSU_PORT", "8445")))
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
