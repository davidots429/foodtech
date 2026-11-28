"""Opt-in native mTLS listener for the separate fryer broker."""
import os

import uvicorn

from .combi_broker_tls import broker_server_config


def main():
    if os.getenv("FOODTECH_TONKATSU_BROKER_ENABLED") != "true":
        raise ValueError("fryer broker disabled")
    from .tonkatsu_broker_app import app

    config = broker_server_config(app, recipe="tonkatsu",
        server_cert=os.environ["FOODTECH_TONKATSU_BROKER_SERVER_CERT"],
        server_key=os.environ["FOODTECH_TONKATSU_BROKER_SERVER_KEY"],
        bff_ca=os.environ["FOODTECH_TONKATSU_BFF_CA"],
        bff_certificate=os.environ["FOODTECH_TONKATSU_BFF_CERT"],
        host=os.getenv("FOODTECH_TONKATSU_BROKER_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("FOODTECH_TONKATSU_BROKER_PORT", "8446")))
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
