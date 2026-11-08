FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/ai-gateway apps/ai-gateway
COPY apps/credential-registration apps/credential-registration
COPY apps/business-api apps/business-api
COPY apps/collection-adapter apps/collection-adapter
COPY apps/standardization-worker apps/standardization-worker
COPY packages/contracts packages/contracts
ARG FOODTECH_PACKAGE
RUN case "$FOODTECH_PACKAGE" in \
      foodtech-ai-gateway|foodtech-credential-registration) ;; \
      *) echo "Unsupported package" >&2; exit 1 ;; \
    esac && uv sync --locked --no-dev --no-editable --package "$FOODTECH_PACKAGE"

FROM python:3.12-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY infra/containers/ai_service_entrypoint.py /app/ai_service_entrypoint.py
USER 10001:10001
ENTRYPOINT ["python", "/app/ai_service_entrypoint.py"]
