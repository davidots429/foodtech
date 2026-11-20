"""Bounded calls to the local Ollama generation API."""

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

CHAT_SYSTEM = ("스마트키친 조리 의사결정 지원 대화입니다. 이전 대화는 참고 자료입니다. "
               "확인되지 않은 측정값을 만들지 말고, 안전 이상은 현장 운영자 확인을 안내하세요. "
               "장비를 직접 제어하거나 권장값을 자동 적용한다고 말하지 마세요. "
               "마지막 사용자 질문에 한국어로 간결하게 답하세요. 합성 작업 맥락이 제공되면 "
               "기록된 상태만 설명하고 누락값을 추정하지 마세요. 조리 설정 변경값이나 "
               "안전 합격을 확정하지 말고 운영자 확인이 필요함을 안내하세요.")


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
    def __init__(self, base_url: str = "http://127.0.0.1:11434", *, timeout: float = 180,
                 transport: httpx.AsyncBaseTransport | None = None):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or \
                parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query:
            raise ValueError("Ollama must be bound to the local loopback address")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def generate(self, model_id: str, prompt: str) -> OllamaResult:
        return await self._request("generate", {"model": model_id, "prompt": prompt}, chat=False)

    async def chat(self, model_id: str, messages: list[dict]) -> OllamaResult:
        return await self._request("chat", {"model": model_id, "messages": [
            {"role": "system", "content": CHAT_SYSTEM}, *messages]}, chat=True)

    async def structured_chat(self, model_id: str, messages: list[dict], schema: dict) -> OllamaResult:
        """Request a schema-constrained reply; the caller must still validate it."""
        return await self._request("chat", {"model": model_id, "messages": messages,
                                             "format": schema}, chat=True)

    async def _request(self, endpoint: str, body: dict, *, chat: bool) -> OllamaResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False,
                                         transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/api/{endpoint}",
                    json={**body, "stream": False,
                          "options": {"num_ctx": 4096, "num_predict": 128}},
                )
            response.raise_for_status()
            value = response.json()
            message = value.get("message") if isinstance(value, dict) and chat else None
            text = message.get("content") if isinstance(message, dict) else value.get("response") if isinstance(value, dict) and not chat else None
            if not isinstance(value, dict) or value.get("done") is not True or \
                    (chat and (not isinstance(message, dict) or message.get("role") != "assistant")) or \
                    not isinstance(text, str):
                raise OllamaFailure("OLLAMA_RESPONSE_INVALID")
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
