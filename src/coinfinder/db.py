"""asyncpg connection pool plus a minimal, transparent migration runner."""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import structlog

from coinfinder.config import get_settings

log = structlog.get_logger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "migrations"

_pool: asyncpg.Pool | None = None


def _normalise(url: str) -> str:
    """Railway hands out postgresql:// URLs; asyncpg rejects the +driver form."""
    return url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgres://", "postgresql://"
    )


async def init_pool(min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            _normalise(settings.database_url),
            min_size=min_size,
            max_size=max_size,
            command_timeout=60,
        )
        log.info("db.pool_ready", max_size=max_size)
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:  # pragma: no cover - misuse guard
        raise RuntimeError("init_pool() must be awaited before pool() is used")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    async with pool().acquire() as conn:
        yield conn


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    async with acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with acquire() as conn:
        return await conn.execute(query, *args)


async def migrate(conn: asyncpg.Connection | None = None) -> list[str]:
    """Apply every migration file that has not run yet, in filename order.

    Each file runs inside its own transaction and is recorded in
    ``schema_migrations``, so re-running is a no-op.
    """
    owns_conn = conn is None
    if conn is None:
        conn = await asyncpg.connect(_normalise(get_settings().database_url))
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        applied: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
            log.info("db.migration_applied", name=path.name)
        return applied
    finally:
        if owns_conn:
            await conn.close()
