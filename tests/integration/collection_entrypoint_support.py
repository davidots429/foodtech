"""Prepare a CA using the container entrypoint for real Mock integration tests."""
import importlib.util
import os
from pathlib import Path
from unittest.mock import patch


def injected_ca(root, temp, certificate, protocol):
    spec = importlib.util.spec_from_file_location('collection_entrypoint', root / 'infra/containers/collection_entrypoint.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = temp / ('injected-' + protocol)
    target.mkdir()
    env = {'FOODTECH_COLLECTION_PROTOCOL': protocol, 'FOODTECH_MOCK_CA_PEM': certificate.read_text()}
    captured = {}
    def record(_program, _argv, child):
        captured.update(child)
    with patch.dict(os.environ, env, clear=True), patch.object(module.sys, 'argv', ['entry']), \
            patch.object(module.tempfile, 'mkdtemp', return_value=str(target)), \
            patch.object(module.os, 'execvpe', side_effect=record):
        module.main()
    if 'FOODTECH_MOCK_CA_PEM' in captured:
        raise AssertionError('CA PEM leaked into child environment')
    variable = {'rest': 'FOODTECH_MOCK_CA_FILE', 'mqtt': 'FOODTECH_MQTT_CA_FILE',
                'opcua-json': 'FOODTECH_MOCK_CA'}[protocol]
    return Path(captured[variable])
