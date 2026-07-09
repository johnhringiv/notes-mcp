# Build stage: install locked dependencies with uv
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Runtime stage
FROM python:3.14-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep nodejs npm \
    && npm install -g prettier@3 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# Runs as fixed UID 1000 (the first regular user on most Linux hosts). Named
# volumes are chowned automatically; only relevant if you bind-mount /repo or
# /data — then the host paths must be writable by UID 1000.
# /data holds OAuth state (signing secret, client registrations).
RUN useradd --uid 1000 --create-home notes \
    && mkdir /repo /data && chown notes:notes /repo /data

COPY --from=builder --chown=notes:notes /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    NOTES_REPO_PATH=/repo \
    OAUTH_STATE_DIR=/data \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

USER notes
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["notes-mcp"]
