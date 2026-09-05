"""GeckoTerminal client - free OHLCV history for backtests.

DexScreener gives an excellent *current* snapshot but no history. The backtest
needs a price path per token, and GeckoTerminal's free tier serves OHLCV
candles without an API key, so it fills that gap.

Rate limit on the free tier is 30 requests/minute, which is the binding
constraint on how fast history can be backfilled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import aiohttp
import orjson
import structlog

from coinfinder.sources.ratelimit import RateLimiter

log = structlog.get_logger(__name__)

BASE_URL = "https://api.geckoterminal.com/api/v2"
_LIMIT = RateLimiter(25, name="geckoterminal")

Timeframe = Literal["minute", "hour", "day"]


@dataclass(slots=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume_usd: float


class GeckoTerminalClient:
    def __init__(self, session: aiohttp.ClientSession | None = None, timeout: float = 25.0):
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def __aenter__(self) -> GeckoTerminalClient:
        await self._ensure()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                trust_env=True,
                headers={"Accept": "application/json;version=20230302"},
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get(self, path: str, *, attempts: int = 3) -> Any:
        session = await self._ensure()
        delay = 2.0
        for attempt in range(attempts):
            await _LIMIT.acquire()
            try:
                async with session.get(f"{BASE_URL}{path}") as resp:
                    if resp.status == 429:
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    if resp.status == 404:
                        return None
                    resp.raise_for_status()
                    return orjson.loads(await resp.read())
            except (TimeoutError, aiohttp.ClientError) as exc:
                log.warning("geckoterminal.error", path=path, attempt=attempt, error=str(exc))
                if attempt == attempts - 1:
                    return None
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def ohlcv(
        self,
        network: str,
        pool_address: str,
        *,
        timeframe: Timeframe = "minute",
        aggregate: int = 5,
        limit: int = 1000,
        before: datetime | None = None,
    ) -> list[Candle]:
        """Fetch candles for a pool, newest first as returned by the API."""
        query = f"?aggregate={aggregate}&limit={min(limit, 1000)}&currency=usd"
        if before is not None:
            query += f"&before_timestamp={int(before.timestamp())}"
        data = await self._get(f"/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}{query}")
        rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        out: list[Candle] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                out.append(
                    Candle(
                        ts=datetime.fromtimestamp(int(row[0]), tz=UTC),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume_usd=float(row[5]),
                    )
                )
            except (TypeError, ValueError):
                continue
        return sorted(out, key=lambda c: c.ts)

    async def pool(self, network: str, pool_address: str) -> dict[str, Any] | None:
        data = await self._get(f"/networks/{network}/pools/{pool_address}")
        return (data or {}).get("data")


def peak_and_path(candles: list[Candle], entry_price: float) -> dict[str, float | None]:
    """Summarise a price path relative to an entry price.

    ``peak_multiple`` uses candle *highs*, which is deliberately optimistic and
    is only ever shown next to realistic exit models, never on its own.
    """
    if not candles or entry_price <= 0:
        return {
            "peak_multiple": None,
            "peak_ts": None,
            "final_multiple": None,
            "min_multiple": None,
        }
    peak = max(candles, key=lambda c: c.high)
    return {
        "peak_multiple": peak.high / entry_price,
        "peak_ts": peak.ts.timestamp(),
        "final_multiple": candles[-1].close / entry_price,
        "min_multiple": min(c.low for c in candles) / entry_price,
    }
