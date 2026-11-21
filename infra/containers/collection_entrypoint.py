"""Materialize an ECS-injected Mock CA before starting a collector."""
import os
import ssl
import sys
import tempfile


def main():
    protocol = os.environ.get('FOODTECH_COLLECTION_PROTOCOL', 'rest')
    settings = {'rest': ('foodtech_collection_adapter', 'FOODTECH_MOCK_CA_FILE'),
                'mqtt': ('foodtech_collection_adapter.mqtt', 'FOODTECH_MQTT_CA_FILE')}
    if protocol not in settings:
        raise ValueError('unsupported collection protocol')
    module, ca_variable = settings[protocol]
    if '--help' not in sys.argv[1:] and '-h' not in sys.argv[1:]:
        pem = os.environ.pop('FOODTECH_MOCK_CA_PEM', '')
        if not pem.startswith('-----BEGIN CERTIFICATE-----') or len(pem.encode('utf-8')) > 65536:
            raise ValueError('valid Mock CA PEM is required')
        directory = tempfile.mkdtemp(prefix='foodtech-mock-ca-')
        os.chmod(directory, 0o700)
        path = os.path.join(directory, 'ca.pem')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(pem)
        ssl.create_default_context(cafile=path)
        os.environ[ca_variable] = path
    os.execvpe('python', ['python', '-m', module, *sys.argv[1:]], os.environ)


if __name__ == '__main__':
    main()
