# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# uv resolves and installs an order of magnitude faster than pip, which
# matters on every Railway redeploy.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

# Dependency layer: only invalidated when the manifest changes.
COPY pyproject.toml README.md ./
RUN uv venv /app/.venv && uv pip install --python /app/.venv/bin/python .

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY web/ ./web/

RUN uv pip install --python /app/.venv/bin/python --no-deps -e . \
    && adduser --disabled-password --gecos "" --uid 10001 app \
    && chown -R app:app /app
USER app

ENV PYTHONPATH=/app/src

EXPOSE 8000

# Runs the API, the ingestion worker and the Telegram bot in one process, and
# applies migrations on startup. Set RUN_COMPONENTS (api / worker / bot) to
# split them across separate services later without changing this image.
CMD ["python", "-m", "coinfinder"]
