"""DexScreener REST client.

DexScreener is the only free source that gives price, liquidity, market cap and
pair age for brand-new tokens across all three chains, so it is the backbone of
token metadata here.

Every field is parsed defensively: the API omits keys for illiquid pairs, and
one missing ``marketCap`` must never take down ingestion.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
import orjson
import structlog

from coinfinder.sources.ratelimit import RateLimiter

log = structlog.get_logger(__name__)

BASE_URL = "https://api.dexscreener.com"

# Published free-tier limits: 300 req/min for pair/token lookups, 60 req/min
# for the discovery endpoints. We stay a little under both.
_PAIR_LIMIT = RateLimiter(240, name="dexscreener.pairs")
_DISCOVERY_LIMIT = RateLimiter(50, name="dexscreener.discovery")


def _f(value: Any) -> float | None:
    """Coerce to float, tolerating strings, None and junk."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None  # reject NaN/inf


def _i(value: Any) -> int | None:
    f = _f(value)
    return int(f) if f is not None else None


@dataclass(slots=True)
class PairInfo:
    chain_slug: str
    dex_id: str | None
    pair_address: str
    base_address: str
    base_symbol: str | None
    base_name: str | None
    quote_address: str | None
    quote_symbol: str | None
    price_usd: float | None
    price_native: float | None
    liquidity_usd: float | None
    fdv_usd: float | None
    mcap_usd: float | None
    volume_24h_usd: float | None
    buys_24h: int | None
    sells_24h: int | None
    price_change_24h: float | None
    created_at: datetime | None

    @property
    def age_minutes(self) -> int | None:
        if self.created_at is None:
            return None
        return max(0, int((datetime.now(UTC) - self.created_at).total_seconds() // 60))

    @property
    def liquidity_to_mcap(self) -> float | None:
        if not self.liquidity_usd or not self.mcap_usd:
            return None
        return self.liquidity_usd / self.mcap_usd


def parse_pair(raw: dict[str, Any]) -> PairInfo | None:
    base = raw.get("baseToken") or {}
    if not base.get("address"):
        return None
    quote = raw.get("quoteToken") or {}
    txns24 = (raw.get("txns") or {}).get("h24") or {}
    created_ms = _i(raw.get("pairCreatedAt"))
    return PairInfo(
        chain_slug=str(raw.get("chainId") or ""),
        dex_id=raw.get("dexId"),
        pair_address=str(raw.get("pairAddress") or "").lower(),
        base_address=str(base["address"]).lower(),
        base_symbol=base.get("symbol"),
        base_name=base.get("name"),
        quote_address=str(quote["address"]).lower() if quote.get("address") else None,
        quote_symbol=quote.get("symbol"),
        price_usd=_f(raw.get("priceUsd")),
        price_native=_f(raw.get("priceNative")),
        liquidity_usd=_f((raw.get("liquidity") or {}).get("usd")),
        fdv_usd=_f(raw.get("fdv")),
        mcap_usd=_f(raw.get("marketCap")) or _f(raw.get("fdv")),
        volume_24h_usd=_f((raw.get("volume") or {}).get("h24")),
        buys_24h=_i(txns24.get("buys")),
        sells_24h=_i(txns24.get("sells")),
        price_change_24h=_f((raw.get("priceChange") or {}).get("h24")),
        created_at=(
            datetime.fromtimestamp(created_ms / 1000, tz=UTC)
            if created_ms and created_ms > 1_000_000_000_000
            else None
        ),
    )


def best_pair(pairs: list[PairInfo], token_address: str) -> PairInfo | None:
    """Pick the pair that best represents a token: deepest liquidity wins."""
    token_address = token_address.lower()
    candidates = [p for p in pairs if p.base_address == token_address]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.liquidity_usd or 0.0)


class DexScreenerClient:
    def __init__(self, session: aiohttp.ClientSession | None = None, timeout: float = 20.0):
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def __aenter__(self) -> DexScreenerClient:
        await self._ensure()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout, trust_env=True)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get(self, path: str, limiter: RateLimiter, *, attempts: int = 3) -> Any:
        session = await self._ensure()
        url = f"{BASE_URL}{path}"
        delay = 1.0
        for attempt in range(attempts):
            await limiter.acquire()
            try:
                async with session.get(url) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    if resp.status == 404:
                        return None
                    resp.raise_for_status()
                    return orjson.loads(await resp.read())
            except (TimeoutError, aiohttp.ClientError) as exc:
                log.warning("dexscreener.error", path=path, attempt=attempt, error=str(exc))
                if attempt == attempts - 1:
                    return None
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def pairs_for_tokens(self, chain_slug: str, addresses: list[str]) -> list[PairInfo]:
        """Look up every pair for up to 30 token addresses in one call."""
        addresses = [a.lower() for a in dict.fromkeys(addresses)][:30]
        if not addresses:
            return []
        data = await self._get(f"/tokens/v1/{chain_slug}/{','.join(addresses)}", _PAIR_LIMIT)
        # This endpoint returns a bare list; the older one wraps it in "pairs".
        raw_pairs = data if isinstance(data, list) else (data or {}).get("pairs") or []
        return [p for p in (parse_pair(r) for r in raw_pairs) if p is not None]

    async def token_pairs_legacy(self, addresses: list[str]) -> list[PairInfo]:
        """Chain-agnostic fallback used when the v1 endpoint misbehaves."""
        addresses = [a.lower() for a in dict.fromkeys(addresses)][:30]
        if not addresses:
            return []
        data = await self._get(f"/latest/dex/tokens/{','.join(addresses)}", _PAIR_LIMIT)
        raw = (data or {}).get("pairs") or []
        return [p for p in (parse_pair(r) for r in raw) if p is not None]

    async def search(self, query: str) -> list[PairInfo]:
        data = await self._get(f"/latest/dex/search?q={query}", _DISCOVERY_LIMIT)
        raw = (data or {}).get("pairs") or []
        return [p for p in (parse_pair(r) for r in raw) if p is not None]

    async def latest_boosted(self) -> list[dict[str, Any]]:
        """Recently boosted tokens - a cheap, free stream of fresh launches."""
        data = await self._get("/token-boosts/latest/v1", _DISCOVERY_LIMIT)
        return data if isinstance(data, list) else []

    async def latest_profiles(self) -> list[dict[str, Any]]:
        data = await self._get("/token-profiles/latest/v1", _DISCOVERY_LIMIT)
        return data if isinstance(data, list) else []
