import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from foodtech_ai_gateway.provider import LiteLLMProvider, ProviderFailure


class LiteLLMProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_per_request_key_and_provider_prefix(self):
        with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
            import litellm
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic reply"))],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3))
        call = AsyncMock(return_value=response)
        with patch.object(litellm, "acompletion", call), \
             patch.object(litellm, "completion_cost", return_value=0.012):
            result = await LiteLLMProvider().generate(
                "google", "gemini-test", "synthetic prompt", bytearray(b"dummy-secret"))
        self.assertEqual(result.text, "synthetic reply")
        self.assertEqual(str(result.actual_cost_usd), "0.012")
        self.assertEqual(call.await_args.kwargs["model"], "gemini/gemini-test")
        self.assertEqual(call.await_args.kwargs["api_key"], "dummy-secret")
        self.assertEqual(call.await_args.kwargs["num_retries"], 0)

    async def test_error_text_and_key_are_not_returned(self):
        with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
            import litellm
        with patch.object(litellm, "acompletion", AsyncMock(
            side_effect=RuntimeError("dummy-secret was rejected"))):
            with self.assertRaises(ProviderFailure) as raised:
                await LiteLLMProvider().generate(
                    "openai", "gpt-test", "synthetic prompt", bytearray(b"dummy-secret"))
        self.assertEqual(str(raised.exception), "PROVIDER_CALL_FAILED")
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
