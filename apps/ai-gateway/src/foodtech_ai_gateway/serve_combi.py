"""Opt-in private Combi listener; invocation remains guarded by its controller."""
import os

import uvicorn
from .combi_tls import combi_server_config


def main():
    if os.getenv('FOODTECH_COMBI_LISTENER_ENABLED') != 'true':
        raise ValueError('Combi listener disabled')
    if not os.getenv('FOODTECH_AI_REGISTRY_TABLE') or not os.getenv('FOODTECH_AUTH_TABLE'):
        raise ValueError('Combi registry and authentication tables required')
    from .main import app
    config = combi_server_config(app, server_cert=os.environ['FOODTECH_COMBI_SERVER_CERT'],
        server_key=os.environ['FOODTECH_COMBI_SERVER_KEY'], broker_ca=os.environ['FOODTECH_COMBI_BROKER_CA'],
        broker_certificate=os.environ['FOODTECH_COMBI_BROKER_CERT'],
        host=os.getenv('FOODTECH_COMBI_BIND_HOST','127.0.0.1'), port=int(os.getenv('FOODTECH_COMBI_PORT','8444')))
    uvicorn.Server(config).run()


if __name__ == '__main__': main()
