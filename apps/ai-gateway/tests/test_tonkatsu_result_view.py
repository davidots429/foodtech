import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_ai_gateway.policy import Execution, ExecutionSnapshot, Feature
from foodtech_ai_gateway.registry import RegistryConflict
from foodtech_ai_gateway.tonkatsu_invocation import read_tonkatsu_request
from foodtech_contracts.dynamo_tonkatsu_scope import TonkatsuScopeUnavailable
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import TonkatsuBaseline, validate
from foodtech_contracts.tonkatsu_execution_binding import TonkatsuExecutionBinding, TonkatsuJobReference


class TonkatsuStoredResultTests(unittest.TestCase):
    def setUp(self):
        current, prior, self.identifier = (str(uuid4()) for _ in range(3))
        self.binding = TonkatsuExecutionBinding(TonkatsuJobReference(current, 2, 7),
                                               TonkatsuJobReference(prior, 3, 9), 4)
        self.scope = {"execution_id": self.identifier, "owner": "customer", "epoch": 4, "job_id": current,
                      "result_view": True}
        self.execution = Execution(self.identifier, "customer", Feature.RECIPE_CORRECTION, "local", None,
            "SYNTHETIC", Decimal(0), job_id=current, job_version=2, prior_job_id=prior, prior_job_version=3)
        terminal = ExecutionSnapshot(self.execution, "SUCCEEDED", 3)
        result = validate('{"temperature_c":170,"duration_seconds":300}',
                          Evidence("PARTIAL", "PASSED", True, False), TonkatsuBaseline("STANDARD", 300))
        self.result = {"state": "SUCCEEDED", "response_text": json.dumps({**result, "attempts": 1})}
        self.registry = Mock()
        self.registry.tonkatsu_status_snapshot.return_value = (terminal, self.binding, self.result)
        self.resolver = Mock()
        self.resolver.plan_execution.return_value = SimpleNamespace(binding=self.binding)
        self.client = Mock()
        self.runtime = ("role", "role-id", self.client, self.registry)

    def read(self):
        module = "foodtech_ai_gateway.tonkatsu_invocation"
        with patch(f"{module}.configured_recipe_runtime", return_value=self.runtime),                 patch(f"{module}._scope_resolver", return_value=(self.resolver, "account", "dev")):
            return read_tonkatsu_request(**self.scope)

    def test_lost_response_can_be_read_with_current_bound_sources(self):
        payload = self.read()
        self.assertEqual((payload["state"], payload["projection_status"], payload["source_current"]),
                         ("SUCCEEDED", "AVAILABLE", True))
        self.assertEqual(payload["proposal"]["data"], {"temperature_c": "170", "duration_seconds": 300})
        self.assertFalse(payload["execution_available"])
        self.assertEqual(self.resolver.require_active_account.call_count, 2)
        self.registry.reserve.assert_not_called()
        self.client.transact_write_items.assert_not_called()
        self.client.close.assert_called_once()

    def test_stale_sources_keep_terminal_state_and_hide_proposal(self):
        self.resolver.plan_execution.side_effect = TonkatsuScopeUnavailable()
        payload = self.read()
        self.assertEqual((payload["state"], payload["projection_status"], payload["source_current"]),
                         ("SUCCEEDED", "UNAVAILABLE", False))
        self.assertIsNone(payload["proposal"])

    def test_mutated_state_or_expired_epoch_cannot_expose_a_proposal(self):
        initial = self.registry.tonkatsu_status_snapshot.return_value
        self.registry.tonkatsu_status_snapshot.side_effect = [initial, None]
        with self.assertRaises(RegistryConflict):
            self.read()
        self.registry.tonkatsu_status_snapshot.side_effect = None
        self.resolver.require_active_account.side_effect = [None, TonkatsuScopeUnavailable()]
        with self.assertRaises(TonkatsuScopeUnavailable):
            self.read()
