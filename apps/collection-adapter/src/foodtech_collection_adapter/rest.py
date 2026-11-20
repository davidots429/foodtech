"""OAuth client-credentials polling for the synthetic HTTPS Mock."""

import base64
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class PollResult:
    records: tuple[str | bytes, ...]
    next_cursor: int


class RestMockClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, ca_file: str,
                 client: httpx.Client | None = None, profile_id: str = "mockprofile-100"):
        if not base_url.startswith("https://") or not ca_file:
            raise ValueError("HTTPS and a trusted CA file are required")
        if profile_id not in ("mockprofile-100", "mockprofile-101", "mockprofile-110", "mockprofile-120"):
            raise ValueError("unsupported Mock profile")
        self.client = client or httpx.Client(base_url=base_url, verify=ca_file, timeout=5.0)
        self.authorization = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self.token: str | None = None
        self.profile_id = profile_id

    def close(self) -> None:
        self.client.close()

    def _authorize(self) -> None:
        response = self.client.post(
            "/oauth2/token", data={"grant_type": "client_credentials"},
            headers={"Authorization": self.authorization},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("token_type") != "Bearer" or not isinstance(body.get("access_token"), str):
            raise ValueError("invalid token response")
        self.token = body["access_token"]

    def poll(self, cursor: int = 0) -> PollResult:
        if cursor < 0:
            raise ValueError("invalid cursor")
        if self.token is None:
            self._authorize()
        for attempt in range(2):
            try:
                response = self.client.get(
                    "/v1/records", params={"cursor": cursor},
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                if response.status_code == 401 and attempt == 0:
                    self._authorize()
                    continue
                response.raise_for_status()
                if (response.headers.get("X-Contract-Version") != "1.0"
                        or response.headers.get("X-Profile-ID") != self.profile_id
                        or response.headers.get("X-Provenance") != "SYNTHETIC"):
                    raise ValueError("unexpected Mock contract")
                next_cursor = int(response.headers["X-Next-Cursor"])
                if next_cursor < cursor:
                    raise ValueError("cursor moved backwards")
                if self.profile_id == "mockprofile-101":
                    if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/xml":
                        raise ValueError("unexpected XML content type")
                    if not response.content:
                        if next_cursor != cursor:
                            raise ValueError("Mock cursor does not match returned records")
                        return PollResult((), next_cursor)
                    if next_cursor != cursor + 1:
                        raise ValueError("Mock cursor does not match returned records")
                    try:
                        record = response.content.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        record = response.content
                    return PollResult((record,), next_cursor)
                # Split on wire-level NDJSON separators before strict UTF-8 decoding.
                # Invalid records retain their exact bytes for the ingestion quarantine.
                parts = response.content.split(b"\n") if response.content else []
                if parts and parts[-1] == b"":
                    parts.pop()
                records = []
                for part in parts:
                    part = part.removesuffix(b"\r")
                    try:
                        records.append(part.decode("utf-8", errors="strict"))
                    except UnicodeDecodeError:
                        if self.profile_id != "mockprofile-100":
                            raise ValueError("invalid UTF-8 in Mock stream") from None
                        records.append(part)
                if next_cursor != cursor + len(records):
                    raise ValueError("Mock cursor does not match returned records")
                return PollResult(tuple(records), next_cursor)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt:
                    raise
        raise RuntimeError("unreachable")
