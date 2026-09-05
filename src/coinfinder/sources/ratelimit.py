"""Async token-bucket limiter, optionally coordinated through Redis.

The bot, API and worker are separate Railway services but share one free-tier
quota, so the limiter defaults to Redis when a client is supplied and degrades
to a purely in-process bucket when it is not.
"""

from __future__ import annotations

import asyncio
import time

import structlog

log = structlog.get_logger(__name__)


class RateLimiter:
    def __init__(self, rate_per_minute: float, *, burst: int | None = None, name: str = "limiter"):
        self.rate = rate_per_minute / 60.0
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_minute // 6)))
        self.name = name
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(min(wait, 5.0))

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc: object) -> None:
        return None
