"""Bounded LiteLLM calls with per-request keys and no provider error text."""

import math
import os
from dataclasses import dataclass
from decimal import Decimal


class ProviderFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderResult:
    text: str
    actual_cost_usd: Decimal
    prompt_tokens: int | None
    output_tokens: int | None


class LiteLLMProvider:
    async def generate(self, provider: str, model_id: str, prompt: str,
                       api_key: bytearray) -> ProviderResult:
        if provider not in {"openai", "google", "anthropic"} or not model_id or \
                "/" in model_id or len(model_id) > 128:
            raise ProviderFailure("PROVIDER_MODEL_INVALID")
        # The model catalog is pinned to the installed package. Startup must not
        # make an unapproved extra request to GitHub for cost metadata.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm

        litellm.suppress_debug_info = True
        litellm.turn_off_message_logging = True
        prefix = "gemini" if provider == "google" else provider
        try:
            response = await litellm.acompletion(
                model=f"{prefix}/{model_id}", messages=[{"role": "user", "content": prompt}],
                api_key=api_key.decode("utf-8"), timeout=180, num_retries=0,
                max_tokens=128, stream=False)
            text = response.choices[0].message.content
            if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 16384:
                raise ProviderFailure("PROVIDER_RESPONSE_INVALID")
            cost = litellm.completion_cost(completion_response=response)
            if cost is None or not math.isfinite(cost) or cost < 0:
                raise ProviderFailure("PROVIDER_COST_UNAVAILABLE")
            usage = getattr(response, "usage", None)
            return ProviderResult(text, Decimal(str(cost)),
                                  getattr(usage, "prompt_tokens", None),
                                  getattr(usage, "completion_tokens", None))
        except ProviderFailure:
            raise
        except litellm.AuthenticationError:
            raise ProviderFailure("PROVIDER_AUTH_FAILED") from None
        except litellm.RateLimitError:
            raise ProviderFailure("PROVIDER_RATE_LIMITED") from None
        except litellm.Timeout:
            raise ProviderFailure("PROVIDER_TIMEOUT") from None
        except Exception:
            raise ProviderFailure("PROVIDER_CALL_FAILED") from None
