# syntax=docker/dockerfile:1

# --- Build: resolve the locked dependencies into /app/.venv -----------------
FROM ghcr.io/astral-sh/uv:0.13.5-python3.13-trixie-slim AS builder

# Use the image's own Python (not a uv download) so the venv still works in
# the runtime stage, which has the same /usr/local/bin/python3.13.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable


# --- Runtime -----------------------------------------------------------------
FROM python:3.13-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Claude Code refuses bypassPermissions as root, and tools shouldn't run as
# root anyway. Match UID/GID to the owner of the mounted workspace.
ARG UID=1000
ARG GID=1000
RUN groupadd --gid "${GID}" agent \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /bin/bash agent \
    && mkdir -p /home/agent/.claude /workspace \
    && chown agent:agent /home/agent/.claude /workspace

# Only the venv: the project is installed into it as a regular package. The
# Claude CLI is bundled in the claude-agent-sdk wheel (pinned by uv.lock).
COPY --from=builder /app/.venv /app/.venv

# Expose the bundled CLI as `claude` for shells inside the container
# (e.g. `docker compose exec claude-xmpp claude`). Fails the build if the
# SDK ever moves it.
RUN cli="$(echo /app/.venv/lib/python3.*/site-packages/claude_agent_sdk/_bundled/claude)" \
    && test -x "$cli" \
    && ln -s "$cli" /usr/local/bin/claude

ENV PATH=/app/.venv/bin:$PATH \
    HOME=/home/agent \
    DISABLE_AUTOUPDATER=1 \
    PYTHONUNBUFFERED=1

USER agent
WORKDIR /workspace

# Exec form, so SIGTERM reaches the bot's signal handler.
ENTRYPOINT ["claude-xmpp"]
CMD ["--config", "/config/config.toml"]
