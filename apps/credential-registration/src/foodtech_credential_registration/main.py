"""Business API authenticated, mTLS-only credential registration endpoint."""

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .registration import CredentialRegistrar, RegistrationError, from_env


app = FastAPI(title="Foodtech Credential Registration")


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EncryptedCredential(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=32)
    key_version: str = Field(min_length=1, max_length=32)
    ciphertext_base64: str = Field(min_length=1, max_length=1024)


def _internal(request: Request) -> None:
    if request.scope.get("scheme") != "https" or not getattr(
        request.app.state, "mtls_listener", False
    ):
        raise HTTPException(401, "internal mTLS authentication required")


def _registrar(request: Request) -> CredentialRegistrar:
    override = getattr(request.app.state, "credential_registrar", None)
    if override is not None:
        return override
    try:
        return from_env()
    except (KeyError, ValueError):
        raise HTTPException(503, "credential registration is not configured") from None


@app.get("/internal/credentials/transfer-key")
async def transfer_key(request: Request, response: Response) -> dict:
    _internal(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return _registrar(request).transfer.public_descriptor()
    except RegistrationError as error:
        raise HTTPException(503, error.code) from None


@app.post("/internal/credentials", status_code=201)
async def register(request: Request, body: EncryptedCredential, response: Response) -> dict:
    _internal(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        credential_id = _registrar(request).register(
            body.tenant_id, body.provider, body.key_version, body.ciphertext_base64)
    except RegistrationError as error:
        raise HTTPException(400, error.code) from None
    return {"credential_id": credential_id, "provider": body.provider, "state": "ACTIVE"}
