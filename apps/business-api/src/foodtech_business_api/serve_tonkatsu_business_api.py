"""Opt-in launcher for the customer API with both Tonkatsu TLS connections."""
import os

import uvicorn
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import Settings
from .main import app as business_app


class CustomerSurface:
    def __init__(self, downstream: ASGIApp) -> None:
        self.downstream = downstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.downstream(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        path = scope.get("path", "")
        if path.startswith(("/api/customer/", "/api/auth/customer/")):
            await self.downstream(scope, receive, send)
            return
        response = JSONResponse({"detail": "Not Found"}, status_code=404, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)


app = CustomerSurface(business_app)


def main() -> None:
    if not all(os.getenv(name) == "true" for name in (
        "FOODTECH_TONKATSU_CUSTOMER_INVOCATION_ENABLED",
        "FOODTECH_TONKATSU_GATEWAY_RECORDS_ENABLED",
        "FOODTECH_PRINCIPAL_PROVISIONING_ENABLED",
    )):
        raise ValueError("protected Tonkatsu customer paths are required")
    if not Settings.from_env().secure_cookies or not os.getenv("FOODTECH_COOKING_TABLE"):
        raise ValueError("secure cookies and a cooking table are required")
    port = int(os.getenv("FOODTECH_TONKATSU_BUSINESS_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid business API port")
    uvicorn.run("foodtech_business_api.serve_tonkatsu_business_api:app",
                host=os.getenv("FOODTECH_TONKATSU_BUSINESS_BIND_HOST", "127.0.0.1"),
                port=port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
