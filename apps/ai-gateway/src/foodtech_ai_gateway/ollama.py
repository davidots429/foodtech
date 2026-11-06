"""Bounded calls to the local Ollama generation API."""

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


class OllamaFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OllamaResult:
    text: str
    prompt_tokens: int | None
    output_tokens: int | None


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434", *, timeout: float = 60,
                 transport: httpx.AsyncBaseTransport | None = None):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or \
                parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query:
            raise ValueError("Ollama must be bound to the local loopback address")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def generate(self, model_id: str, prompt: str) -> OllamaResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False,
                                         transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": model_id, "prompt": prompt, "stream": False,
                          "options": {"num_ctx": 4096}},
                )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get("done") is not True or \
                    not isinstance(value.get("response"), str):
                raise OllamaFailure("OLLAMA_RESPONSE_INVALID")
            text = value["response"]
            if len(text.encode("utf-8")) > 16384:
                raise OllamaFailure("OLLAMA_RESPONSE_TOO_LARGE")
            return OllamaResult(text, value.get("prompt_eval_count"), value.get("eval_count"))
        except httpx.TimeoutException as error:
            raise OllamaFailure("OLLAMA_TIMEOUT") from error
        except httpx.ConnectError as error:
            raise OllamaFailure("OLLAMA_UNAVAILABLE") from error
        except httpx.HTTPStatusError as error:
            raise OllamaFailure("OLLAMA_HTTP_ERROR") from error
        except httpx.RequestError as error:
            raise OllamaFailure("OLLAMA_TRANSPORT_ERROR") from error
        except (ValueError, TypeError) as error:
            raise OllamaFailure("OLLAMA_RESPONSE_INVALID") from error
