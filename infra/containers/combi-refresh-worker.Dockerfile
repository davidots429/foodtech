FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/ai-gateway apps/ai-gateway
COPY apps/credential-registration apps/credential-registration
COPY apps/business-api apps/business-api
COPY apps/capsule-crypto apps/capsule-crypto
COPY apps/collection-adapter apps/collection-adapter
COPY apps/standardization-worker apps/standardization-worker
COPY packages/contracts packages/contracts
RUN uv sync --locked --no-dev --no-editable --package foodtech-business-api

FROM python:3.12-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 10001:10001
ENTRYPOINT ["python", "-m", "foodtech_business_api.combi_refresh_worker"]
