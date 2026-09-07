# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# agent-relay — multi-stage image.
#
#   stage 1 (builder): uv resolves + installs the locked deps into /app/.venv
#   stage 2 (runtime): plain python-slim, gets only the finished venv
#
# The dependency layer is built from pyproject.toml + uv.lock alone, so editing
# source code never re-resolves or re-downloads anything.
# ---------------------------------------------------------------------------

# --- stage 1: build the virtualenv ----------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency-only layer: cached until uv.lock / pyproject.toml change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project --no-editable

# Now the source, and install the project itself on top of the cached deps.
# --no-editable makes /app/.venv self-contained so the runtime stage needs
# nothing else from this build context.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Note on AGENT_RELAY_DB_URL below: four slashes. "sqlite:////data/agent_relay.db"
# is the ABSOLUTE path /data/agent_relay.db, which is where the volume is mounted.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    AGENT_RELAY_HOST=0.0.0.0 \
    AGENT_RELAY_PORT=8077 \
    AGENT_RELAY_LOG_LEVEL=INFO \
    AGENT_RELAY_DB_URL=sqlite:////data/agent_relay.db

# Non-root runtime user.
RUN groupadd --system --gid 1000 relay \
    && useradd --system --uid 1000 --gid relay --create-home --home-dir /home/relay relay \
    && mkdir -p /app /data \
    && chown -R relay:relay /app /data

WORKDIR /app

COPY --from=builder --chown=relay:relay /app/.venv /app/.venv

# Mount a volume here (compose does) so the SQLite file outlives the container.
VOLUME ["/data"]

USER relay

EXPOSE 8077

# stdlib-only probe: no curl/wget in the image just to answer this question.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8077/health', timeout=4).status == 200 else 1)"]

# Bind 0.0.0.0 so the port is reachable from outside the container.
CMD ["uvicorn", "agent_relay.main:app", "--host", "0.0.0.0", "--port", "8077"]
