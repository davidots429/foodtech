import unittest
from unittest.mock import patch

from foodtech_ai_gateway import serve_combi


class CombiTlsStartupTests(unittest.TestCase):
    def test_default_entry_does_not_load_config_or_start_listener(self):
        with patch.dict('os.environ',{},clear=True),patch.object(serve_combi,'combi_server_config') as configure,patch.object(serve_combi.uvicorn,'Server') as server:
            with self.assertRaisesRegex(ValueError,'Combi listener disabled'):serve_combi.main()
            configure.assert_not_called();server.assert_not_called()

    def test_missing_tables_do_not_start_opted_in_listener(self):
        with patch.dict('os.environ',{'FOODTECH_COMBI_LISTENER_ENABLED':'true'},clear=True),patch.object(serve_combi,'combi_server_config') as configure:
            with self.assertRaisesRegex(ValueError,'registry and authentication tables required'):serve_combi.main()
            configure.assert_not_called()
