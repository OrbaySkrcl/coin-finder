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

# Railway injects PORT; the api process reads it via API_PORT.
EXPOSE 8000

# Default to the API. railway.toml overrides the command for the worker and
# bot services, which share this image.
CMD ["sh", "-c", "python scripts/migrate.py && python -m uvicorn coinfinder.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
