FROM python:3.12-slim-trixie AS base
# Debian 13 stable security floor; do not use unstable repositories.
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
RUN uv sync --locked --no-dev --no-editable --package foodtech-collection-adapter

FROM base
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY infra/containers/collection_entrypoint.py /app/collection_entrypoint.py
# ECS copies this volume path's ownership; its default root:0755 is not writable
# by the collector. Keep the rest of the filesystem read-only at runtime.
RUN mkdir -p /run/foodtech-collection \
    && touch /run/foodtech-collection/.keep \
    && chown -R 10001:10001 /run/foodtech-collection \
    && chmod 0700 /run/foodtech-collection
ENV TMPDIR=/run/foodtech-collection
VOLUME ["/run/foodtech-collection"]
USER 10001:10001
ENTRYPOINT ["python", "/app/collection_entrypoint.py"]
