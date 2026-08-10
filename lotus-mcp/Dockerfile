# Lotus MCP adapter.
#
# The image ships no network service. The MCP server speaks stdio and is
# spawned per client session; the default command is an idle maintenance
# process whose only jobs are to hold the volumes and answer the health check.

# ---- builder: resolve dependencies into a self-contained venv ----
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# ---- runtime ----
FROM python:3.12-slim

LABEL org.opencontainers.image.title="lotus-mcp" \
      org.opencontainers.image.description="Local-first, read-only MCP adapter over user-owned mood exports." \
      org.opencontainers.image.licenses="MIT"

# Fixed non-root UID/GID. Compose overrides `user:` to match whoever owns the
# host directories — see compose.yaml and the ZimaOS notes in README.md.
RUN groupadd --gid 10001 lotus \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin lotus

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOTUS_CONFIG=/app/config/config.yaml \
    LOTUS_DATA_DIR=/app/data \
    LOTUS_IMPORT_ROOT=/app/imports

WORKDIR /app

# Only these paths are ever written. Everything else can be mounted read-only
# (compose sets read_only: true on the container filesystem).
RUN mkdir -p /app/config /app/secrets /app/data \
             /app/imports/incoming /app/imports/processed /app/imports/failed /app/logs \
    && chown -R 10001:10001 /app

# Shipped for reference. It is NOT loaded automatically — mount your own
# config.yaml over /app/config. With no config file present the server runs on
# built-in defaults, which are identical to this example and deny every
# sensitive access path.
COPY --chown=10001:10001 config/config.example.yaml /app/config/config.example.yaml

USER 10001:10001

# Reports schema version and row counts. Never mood content.
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD ["lotus-mcp", "health"]

# Idle maintenance process. To run the MCP server itself:
#   docker compose run --rm -T lotus-mcp lotus-mcp serve
CMD ["lotus-mcp", "maintain"]
