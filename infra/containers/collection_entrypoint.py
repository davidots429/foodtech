"""Materialize an ECS-injected Mock CA before starting a collector."""
import os
import ssl
import sys
import tempfile
import base64

from cryptography import x509
from cryptography.hazmat.primitives import serialization


def write_private(directory, name, content):
    path = os.path.join(directory, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(content)
    return path


def main():
    protocol = os.environ.get('FOODTECH_COLLECTION_PROTOCOL', 'rest')
    settings = {'rest': ('foodtech_collection_adapter', 'FOODTECH_MOCK_CA_FILE'),
                'mqtt': ('foodtech_collection_adapter.mqtt', 'FOODTECH_MQTT_CA_FILE'),
                'opcua-json': ('foodtech_collection_adapter.opcua_json', 'FOODTECH_MOCK_CA'),
                'opcua-binary': ('foodtech_collection_adapter.opcua', None)}
    if protocol not in settings:
        raise ValueError('unsupported collection protocol')
    module, ca_variable = settings[protocol]
    if '--help' not in sys.argv[1:] and '-h' not in sys.argv[1:] and protocol == 'opcua-binary':
        values = [os.environ.pop(name, '') for name in
                  ('FOODTECH_OPCUA_CLIENT_CERT_PEM', 'FOODTECH_OPCUA_CLIENT_KEY_PEM', 'FOODTECH_OPCUA_SERVER_CERT_DER_BASE64')]
        if any(not value or len(value.encode('utf-8')) > 65536 for value in values):
            raise ValueError('OPC UA certificate material is required')
        try:
            certificate = x509.load_pem_x509_certificate(values[0].encode('ascii'))
            key = serialization.load_pem_private_key(values[1].encode('ascii'), password=None)
            server = base64.b64decode(values[2], validate=True)
            x509.load_der_x509_certificate(server)
            public_format = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            if certificate.public_key().public_bytes(*public_format) != key.public_key().public_bytes(*public_format):
                raise ValueError('certificate and key do not match')
        except (ValueError, TypeError, UnicodeError):
            raise ValueError('invalid OPC UA certificate material') from None
        directory = tempfile.mkdtemp(prefix='foodtech-opcua-')
        os.chmod(directory, 0o700)
        for variable, name, content in (('FOODTECH_OPCUA_CLIENT_CERT', 'client.pem', values[0].encode('ascii')),
                ('FOODTECH_OPCUA_CLIENT_KEY', 'client-key.pem', values[1].encode('ascii')),
                ('FOODTECH_OPCUA_SERVER_CERT', 'server.der', server)):
            os.environ[variable] = write_private(directory, name, content)
    elif '--help' not in sys.argv[1:] and '-h' not in sys.argv[1:]:
        pem = os.environ.pop('FOODTECH_MOCK_CA_PEM', '')
        if not pem.startswith('-----BEGIN CERTIFICATE-----') or len(pem.encode('utf-8')) > 65536:
            raise ValueError('valid Mock CA PEM is required')
        directory = tempfile.mkdtemp(prefix='foodtech-mock-ca-')
        os.chmod(directory, 0o700)
        path = write_private(directory, 'ca.pem', pem.encode('utf-8'))
        ssl.create_default_context(cafile=path)
        os.environ[ca_variable] = path
    os.execvpe('python', ['python', '-m', module, *sys.argv[1:]], os.environ)


if __name__ == '__main__':
    main()
