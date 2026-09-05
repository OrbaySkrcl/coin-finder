"""Signal outcome tracking.

Backtests are only as honest as this module. Two things matter:

* **Horizons are written once.** ``mult_1h`` is filled when the signal turns an
  hour old and never touched again, so a backtest asking "what would a 1-hour
  stop have returned" gets the answer as it was, not as it looks today.
* **Delisted tokens are recorded as dead, not deleted.** A rug that vanishes
  from DexScreener is a -100% trade. Dropping the row would quietly turn every
  aggregate into a survivorship illusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from coinfinder import db
from coinfinder.chains import Chain
from coinfinder.sources.dexscreener import DexScreenerClient, best_pair

log = structlog.get_logger(__name__)

HORIZONS: tuple[tuple[str, timedelta], ...] = (
    ("15m", timedelta(minutes=15)),
    ("1h", timedelta(hours=1)),
    ("4h", timedelta(hours=4)),
    ("24h", timedelta(hours=24)),
    ("7d", timedelta(days=7)),
)

#: Under this, a pool cannot be exited: the position is written off.
DEAD_LIQUIDITY_USD = 500.0


@dataclass(slots=True)
class OutcomeUpdate:
    signal_id: int
    last_price_usd: float | None
    current_multiple: float | None
    peak_multiple: float | None
    is_dead: bool
    horizons: dict[str, float]


async def open_signals(chain: Chain, *, max_age_days: int = 30) -> list[dict[str, Any]]:
    """Signals still worth tracking on this chain."""
    rows = await db.fetch(
        """
        SELECT s.id, s.token, s.ts, s.snap_price_usd,
               o.peak_multiple, o.mult_15m, o.mult_1h, o.mult_4h, o.mult_24h, o.mult_7d
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        WHERE s.chain_id = $1
          AND s.ts >= $2
          AND s.snap_price_usd > 0
          AND (o.is_dead IS NULL OR o.is_dead = FALSE)
        ORDER BY s.ts DESC
        """,
        chain.chain_id,
        datetime.now(UTC) - timedelta(days=max_age_days),
    )
    return [dict(r) for r in rows]


def compute_update(
    signal: dict[str, Any],
    *,
    price_usd: float | None,
    liquidity_usd: float | None,
    listed: bool,
    now: datetime,
) -> OutcomeUpdate:
    """Derive a signal's outcome from its current market state."""
    entry = float(signal["snap_price_usd"])
    is_dead = (
        not listed
        or price_usd is None
        or price_usd <= 0
        or (liquidity_usd is not None and liquidity_usd < DEAD_LIQUIDITY_USD)
    )
    current = 0.0 if is_dead else (price_usd or 0.0) / entry

    prior_peak = signal.get("peak_multiple")
    peak = max(float(prior_peak or 0.0), current)

    # Fill each horizon exactly once, the first time we look after it passes.
    age = now - signal["ts"]
    horizons: dict[str, float] = {}
    for name, delta in HORIZONS:
        if signal.get(f"mult_{name}") is None and age >= delta:
            horizons[name] = current

    return OutcomeUpdate(
        signal_id=int(signal["id"]),
        last_price_usd=price_usd,
        current_multiple=current,
        peak_multiple=peak,
        is_dead=is_dead,
        horizons=horizons,
    )


async def persist(updates: list[OutcomeUpdate]) -> int:
    if not updates:
        return 0
    async with db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO signal_outcomes (signal_id, updated_at, last_price_usd,
                current_multiple, peak_multiple, peak_at, drawdown_from_peak, is_dead,
                mult_15m, mult_1h, mult_4h, mult_24h, mult_7d)
            -- Casts are explicit: without them Postgres deduces $3 and $4 as
            -- integer from the "> 0" comparison and double from the division,
            -- and refuses the statement as ambiguous.
            VALUES ($1, now(), $2::numeric, $3::double precision, $4::double precision,
                    CASE WHEN $4::double precision > 0 THEN now() ELSE NULL END,
                    CASE WHEN $4::double precision > 0
                         THEN 1 - ($3::double precision / $4::double precision)
                         ELSE NULL END,
                    $5::boolean, $6::double precision, $7::double precision,
                    $8::double precision, $9::double precision, $10::double precision)
            ON CONFLICT (signal_id) DO UPDATE SET
                updated_at = now(),
                last_price_usd = EXCLUDED.last_price_usd,
                current_multiple = EXCLUDED.current_multiple,
                peak_multiple = GREATEST(
                    COALESCE(signal_outcomes.peak_multiple, 0), EXCLUDED.peak_multiple
                ),
                peak_at = CASE
                    WHEN EXCLUDED.peak_multiple > COALESCE(signal_outcomes.peak_multiple, 0)
                    THEN now() ELSE signal_outcomes.peak_at END,
                drawdown_from_peak = CASE
                    WHEN GREATEST(
                        COALESCE(signal_outcomes.peak_multiple, 0), EXCLUDED.peak_multiple
                    ) > 0
                    THEN 1 - (EXCLUDED.current_multiple / GREATEST(
                        COALESCE(signal_outcomes.peak_multiple, 0), EXCLUDED.peak_multiple
                    ))
                    ELSE NULL END,
                is_dead = EXCLUDED.is_dead,
                -- COALESCE keeps the first value written at each horizon.
                mult_15m = COALESCE(signal_outcomes.mult_15m, EXCLUDED.mult_15m),
                mult_1h  = COALESCE(signal_outcomes.mult_1h,  EXCLUDED.mult_1h),
                mult_4h  = COALESCE(signal_outcomes.mult_4h,  EXCLUDED.mult_4h),
                mult_24h = COALESCE(signal_outcomes.mult_24h, EXCLUDED.mult_24h),
                mult_7d  = COALESCE(signal_outcomes.mult_7d,  EXCLUDED.mult_7d)
            """,
            [
                (
                    u.signal_id,
                    u.last_price_usd,
                    u.current_multiple,
                    u.peak_multiple,
                    u.is_dead,
                    u.horizons.get("15m"),
                    u.horizons.get("1h"),
                    u.horizons.get("4h"),
                    u.horizons.get("24h"),
                    u.horizons.get("7d"),
                )
                for u in updates
            ],
        )
    return len(updates)


async def refresh(client: DexScreenerClient, chain: Chain, *, batch: int = 30) -> int:
    """Refresh every tracked signal on one chain."""
    signals = await open_signals(chain)
    if not signals:
        return 0

    by_token: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        by_token.setdefault(signal["token"], []).append(signal)

    now = datetime.now(UTC)
    tokens = list(by_token)
    updates: list[OutcomeUpdate] = []
    market_rows: list[dict[str, Any]] = []

    for start in range(0, len(tokens), batch):
        group = tokens[start : start + batch]
        pairs = await client.pairs_for_tokens(chain.dexscreener_slug, group)
        for token in group:
            pair = best_pair(pairs, token)
            listed = pair is not None and bool(pair.price_usd)
            for signal in by_token[token]:
                updates.append(
                    compute_update(
                        signal,
                        price_usd=pair.price_usd if pair else None,
                        liquidity_usd=pair.liquidity_usd if pair else None,
                        listed=listed,
                        now=now,
                    )
                )
            market_rows.append(
                {
                    "address": token,
                    "price_usd": pair.price_usd if pair else None,
                    "mcap_usd": pair.mcap_usd if pair else None,
                    "fdv_usd": pair.fdv_usd if pair else None,
                    "liquidity_usd": pair.liquidity_usd if pair else None,
                    "volume_24h_usd": pair.volume_24h_usd if pair else None,
                    "buys_24h": pair.buys_24h if pair else None,
                    "sells_24h": pair.sells_24h if pair else None,
                    "is_delisted": not listed,
                }
            )

    from coinfinder import repo

    await repo.upsert_market(chain.chain_id, market_rows)
    await repo.append_price_history(
        chain.chain_id,
        [
            {
                "address": row["address"],
                "ts": now,
                "price_usd": row["price_usd"],
                "mcap_usd": row["mcap_usd"],
                "liquidity_usd": row["liquidity_usd"],
            }
            for row in market_rows
            if row["price_usd"]
        ],
    )
    written = await persist(updates)
    log.info("outcomes.refreshed", chain=chain.key, signals=written, tokens=len(tokens))
    return written
