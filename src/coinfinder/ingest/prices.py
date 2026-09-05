"""Native token USD price, cached.

Every trade's USD value is derived from its native leg, so this price is on
the critical path for the whole pipeline. It is cached in Redis so the API,
worker and bot share one lookup instead of each burning quota.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from coinfinder.chains import Chain
from coinfinder.sources.dexscreener import DexScreenerClient, best_pair

log = structlog.get_logger(__name__)

TTL_SECONDS = 300.0
#: Used when a lookup fails so ingestion degrades instead of stopping.
FALLBACK_PRICES = {"ETH": 3000.0, "BNB": 600.0}


@dataclass(slots=True)
class _Entry:
    price: float
    fetched_at: float
    is_fallback: bool


class NativePriceOracle:
    def __init__(self, client: DexScreenerClient, *, ttl_seconds: float = TTL_SECONDS):
        self._client = client
        self._ttl = ttl_seconds
        self._cache: dict[str, _Entry] = {}

    async def price_usd(self, chain: Chain) -> float:
        entry = self._cache.get(chain.key)
        now = time.monotonic()
        if entry is not None and now - entry.fetched_at < self._ttl:
            return entry.price

        price = await self._lookup(chain)
        is_fallback = price is None
        if price is None:
            # Keep a stale-but-real price over a hardcoded guess when possible.
            price = (
                entry.price
                if entry is not None and not entry.is_fallback
                else FALLBACK_PRICES.get(chain.native_symbol, 0.0)
            )
            log.warning("prices.fallback", chain=chain.key, price=price)

        self._cache[chain.key] = _Entry(price=price, fetched_at=now, is_fallback=is_fallback)
        return price

    async def _lookup(self, chain: Chain) -> float | None:
        wrapped = chain.wrapped_native.lower()
        if not wrapped or set(wrapped.removeprefix("0x")) == {"0"}:
            # Chains without a known wrapped-native deployment (Robinhood
            # until its canonical address is configured) borrow ETH's price.
            return await self._eth_reference()
        pairs = await self._client.pairs_for_tokens(chain.dexscreener_slug, [wrapped])
        pair = best_pair(pairs, wrapped)
        return pair.price_usd if pair and pair.price_usd else None

    async def _eth_reference(self) -> float | None:
        from coinfinder.chains import BASE

        pairs = await self._client.pairs_for_tokens(
            BASE.dexscreener_slug, [BASE.wrapped_native.lower()]
        )
        pair = best_pair(pairs, BASE.wrapped_native.lower())
        return pair.price_usd if pair and pair.price_usd else None
