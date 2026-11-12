# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# --locked installs exactly uv.lock and fails if it is stale, so a commit always builds
# the same dependency set.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Run as a non-root user.
RUN useradd --create-home --uid 10001 app
USER app

# The image tag the build pushes, which the server reports as its version. Declared
# after the install so a new tag doesn't invalidate the cached layers above.
ARG RAPIDATA_MCP_IMAGE_TAG=""
ENV RAPIDATA_MCP_IMAGE_TAG=${RAPIDATA_MCP_IMAGE_TAG}

EXPOSE 8000

# Liveness/readiness: GET /health is unauthenticated and returns
# {"status":"ok","version":"<image tag> · rapidata <sdk version>"}.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"

CMD ["rapidata-mcp"]
