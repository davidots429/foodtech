FROM python:3.12-slim-trixie AS base
# DSA-6657-1: use Debian 13's security update, without unstable repositories.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade libzstd1 \
    && dpkg --compare-versions "$(dpkg-query --show --showformat='${Version}' libzstd1)" ge '1.5.7+dfsg-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder
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
ARG FOODTECH_PACKAGE=foodtech-business-api
RUN case "$FOODTECH_PACKAGE" in foodtech-business-api|foodtech-standardization-worker) ;; *) exit 1 ;; esac \
    && uv sync --locked --no-dev --no-editable --package "$FOODTECH_PACKAGE"

FROM base AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 10001:10001
FROM runtime AS standardization
ENTRYPOINT ["python", "-m", "foodtech_standardization_worker.runtime"]

FROM runtime AS maintenance
ENTRYPOINT ["python", "-m", "foodtech_business_api.combi_refresh_worker"]
