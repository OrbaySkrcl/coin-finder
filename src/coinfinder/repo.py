"""All SQL lives here so query shapes stay reviewable in one place."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson

from coinfinder import db
from coinfinder.ingest.extract import RawTrade


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# --- checkpoints -------------------------------------------------------


async def get_checkpoint(chain_id: int, job: str) -> int:
    row = await db.fetchval(
        "SELECT last_block FROM ingest_checkpoints WHERE chain_id = $1 AND job = $2",
        chain_id,
        job,
    )
    return int(row or 0)


async def set_checkpoint(chain_id: int, job: str, block: int) -> None:
    await db.execute(
        """
        INSERT INTO ingest_checkpoints (chain_id, job, last_block, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (chain_id, job)
        DO UPDATE SET last_block = EXCLUDED.last_block, updated_at = now()
        """,
        chain_id,
        job,
        block,
    )


# --- wallets -----------------------------------------------------------


async def watched_wallets(chain_id: int) -> list[str]:
    rows = await db.fetch(
        """
        SELECT address FROM wallets
        WHERE chain_id = $1 AND watch_since IS NOT NULL AND NOT is_excluded AND NOT is_contract
        ORDER BY address
        """,
        chain_id,
    )
    return [r["address"] for r in rows]


async def upsert_wallets(chain_id: int, addresses: list[str], *, watch: bool = False) -> None:
    if not addresses:
        return
    await db.execute(
        """
        INSERT INTO wallets (chain_id, address, watch_since)
        SELECT $1, addr, CASE WHEN $3 THEN now() ELSE NULL END
        FROM unnest($2::text[]) AS addr
        ON CONFLICT (chain_id, address) DO UPDATE
        SET watch_since = COALESCE(wallets.watch_since, EXCLUDED.watch_since)
        """,
        chain_id,
        [a.lower() for a in addresses],
        watch,
    )


async def mark_contracts(chain_id: int, addresses: list[str]) -> None:
    if not addresses:
        return
    await db.execute(
        """
        UPDATE wallets SET is_contract = TRUE, is_excluded = TRUE,
               exclude_reason = COALESCE(exclude_reason, 'contract')
        WHERE chain_id = $1 AND address = ANY($2::text[])
        """,
        chain_id,
        [a.lower() for a in addresses],
    )


async def exclude_wallets(chain_id: int, addresses: list[str], reason: str) -> None:
    if not addresses:
        return
    await db.execute(
        """
        UPDATE wallets SET is_excluded = TRUE, exclude_reason = $3
        WHERE chain_id = $1 AND address = ANY($2::text[])
        """,
        chain_id,
        [a.lower() for a in addresses],
        reason,
    )


async def set_watchlist(chain_id: int, addresses: list[str]) -> int:
    """Make exactly ``addresses`` the watched set for this chain."""
    lowered = [a.lower() for a in addresses]
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE wallets SET watch_since = NULL WHERE chain_id = $1 AND watch_since IS NOT NULL",
            chain_id,
        )
        if lowered:
            await conn.execute(
                """
                INSERT INTO wallets (chain_id, address, watch_since)
                SELECT $1, addr, now() FROM unnest($2::text[]) AS addr
                ON CONFLICT (chain_id, address) DO UPDATE SET watch_since = now()
                """,
                chain_id,
                lowered,
            )
    return len(lowered)


# --- tokens ------------------------------------------------------------


async def upsert_tokens(chain_id: int, tokens: list[dict[str, Any]]) -> None:
    if not tokens:
        return
    async with db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO tokens (chain_id, address, symbol, name, decimals, pair_address,
                                dex_id, launched_at, meta_updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
            ON CONFLICT (chain_id, address) DO UPDATE SET
                symbol       = COALESCE(EXCLUDED.symbol, tokens.symbol),
                name         = COALESCE(EXCLUDED.name, tokens.name),
                decimals     = COALESCE(EXCLUDED.decimals, tokens.decimals),
                pair_address = COALESCE(EXCLUDED.pair_address, tokens.pair_address),
                dex_id       = COALESCE(EXCLUDED.dex_id, tokens.dex_id),
                launched_at  = COALESCE(tokens.launched_at, EXCLUDED.launched_at),
                meta_updated_at = now()
            """,
            [
                (
                    chain_id,
                    t["address"].lower(),
                    t.get("symbol"),
                    t.get("name"),
                    t.get("decimals", 18),
                    (t.get("pair_address") or "").lower() or None,
                    t.get("dex_id"),
                    t.get("launched_at"),
                )
                for t in tokens
            ],
        )


async def ensure_token_rows(chain_id: int, addresses: list[str]) -> None:
    """Create placeholder token rows so foreign keys hold before enrichment."""
    if not addresses:
        return
    await db.execute(
        """
        INSERT INTO tokens (chain_id, address)
        SELECT $1, addr FROM unnest($2::text[]) AS addr
        ON CONFLICT DO NOTHING
        """,
        chain_id,
        [a.lower() for a in addresses],
    )


async def upsert_market(chain_id: int, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    async with db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO token_market (chain_id, address, updated_at, price_usd, mcap_usd,
                                      fdv_usd, liquidity_usd, volume_24h_usd, buys_24h,
                                      sells_24h, is_delisted)
            VALUES ($1, $2, now(), $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (chain_id, address) DO UPDATE SET
                updated_at = now(), price_usd = EXCLUDED.price_usd,
                mcap_usd = EXCLUDED.mcap_usd, fdv_usd = EXCLUDED.fdv_usd,
                liquidity_usd = EXCLUDED.liquidity_usd,
                volume_24h_usd = EXCLUDED.volume_24h_usd,
                buys_24h = EXCLUDED.buys_24h, sells_24h = EXCLUDED.sells_24h,
                is_delisted = EXCLUDED.is_delisted
            """,
            [
                (
                    chain_id,
                    r["address"].lower(),
                    _dec(r.get("price_usd")),
                    _dec(r.get("mcap_usd")),
                    _dec(r.get("fdv_usd")),
                    _dec(r.get("liquidity_usd")),
                    _dec(r.get("volume_24h_usd")),
                    r.get("buys_24h"),
                    r.get("sells_24h"),
                    bool(r.get("is_delisted", False)),
                )
                for r in rows
            ],
        )


async def append_price_history(chain_id: int, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    async with db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO price_history (chain_id, address, ts, price_usd, mcap_usd, liquidity_usd)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (chain_id, address, ts) DO NOTHING
            """,
            [
                (
                    chain_id,
                    r["address"].lower(),
                    r["ts"],
                    _dec(r["price_usd"]),
                    _dec(r.get("mcap_usd")),
                    _dec(r.get("liquidity_usd")),
                )
                for r in rows
            ],
        )


# --- trades ------------------------------------------------------------


async def insert_trades(
    chain_id: int, trades: list[RawTrade], native_price_usd: float | None
) -> int:
    """Insert trades idempotently; the primary key makes replays safe."""
    if not trades:
        return 0
    await ensure_token_rows(chain_id, [t.token for t in trades])
    await upsert_wallets(chain_id, [t.wallet for t in trades])
    async with db.acquire() as conn:
        result = await conn.executemany(
            """
            INSERT INTO wallet_trades (chain_id, tx_hash, log_index, wallet, token,
                                       block_number, ts, side, token_amount, usd_value, price_usd)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (chain_id, tx_hash, log_index) DO NOTHING
            """,
            [
                (
                    chain_id,
                    t.tx_hash,
                    t.log_index,
                    t.wallet,
                    t.token,
                    t.block_number,
                    t.ts,
                    t.side,
                    Decimal(t.token_amount),
                    _dec(t.usd_value(native_price_usd)),
                    _dec(
                        (t.usd_value(native_price_usd) or 0) / (t.token_amount / 10**18)
                        if t.token_amount and t.usd_value(native_price_usd)
                        else None
                    ),
                )
                for t in trades
            ],
        )
    _ = result
    return len(trades)


async def recent_buys(
    chain_id: int, since: datetime, *, min_usd: float = 0.0
) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT t.token, t.wallet, t.ts, t.usd_value, t.block_number
        FROM wallet_trades t
        JOIN wallets w ON w.chain_id = t.chain_id AND w.address = t.wallet
        WHERE t.chain_id = $1 AND t.ts >= $2 AND t.side = 'buy'
          AND w.watch_since IS NOT NULL AND NOT w.is_excluded
          AND (t.usd_value IS NULL OR t.usd_value >= $3)
        ORDER BY t.ts
        """,
        chain_id,
        since,
        _dec(min_usd),
    )
    return [dict(r) for r in rows]


async def wallet_trade_history(chain_id: int, days: int) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT wallet, token, ts, side, token_amount, usd_value
        FROM wallet_trades
        WHERE chain_id = $1 AND ts >= $2 AND usd_value IS NOT NULL
        ORDER BY wallet, token, ts
        """,
        chain_id,
        datetime.now(UTC) - timedelta(days=days),
    )
    return [dict(r) for r in rows]


# --- clusters & scores -------------------------------------------------


async def replace_clusters(chain_id: int, mapping: dict[str, str], reason: str) -> None:
    if not mapping:
        return
    async with db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO wallet_clusters (chain_id, wallet, cluster_id, reason, updated_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (chain_id, wallet)
            DO UPDATE SET cluster_id = EXCLUDED.cluster_id, reason = EXCLUDED.reason,
                          updated_at = now()
            """,
            [(chain_id, w, c, reason) for w, c in mapping.items()],
        )


async def cluster_map(chain_id: int) -> dict[str, str]:
    rows = await db.fetch(
        "SELECT wallet, cluster_id FROM wallet_clusters WHERE chain_id = $1", chain_id
    )
    return {r["wallet"]: r["cluster_id"] for r in rows}


async def upsert_scores(chain_id: int, scores: list[dict[str, Any]]) -> None:
    if not scores:
        return
    async with db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO wallet_scores (chain_id, wallet, computed_at, window_days, closed_trades,
                wins, win_rate, median_multiple, realized_pnl_usd, avg_hold_minutes,
                distinct_tokens, score, is_smart)
            VALUES ($1, $2, now(), $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (chain_id, wallet) DO UPDATE SET
                computed_at = now(), window_days = EXCLUDED.window_days,
                closed_trades = EXCLUDED.closed_trades, wins = EXCLUDED.wins,
                win_rate = EXCLUDED.win_rate, median_multiple = EXCLUDED.median_multiple,
                realized_pnl_usd = EXCLUDED.realized_pnl_usd,
                avg_hold_minutes = EXCLUDED.avg_hold_minutes,
                distinct_tokens = EXCLUDED.distinct_tokens, score = EXCLUDED.score,
                is_smart = EXCLUDED.is_smart
            """,
            [
                (
                    chain_id,
                    s["wallet"],
                    s["window_days"],
                    s["closed_trades"],
                    s["wins"],
                    s.get("win_rate"),
                    s.get("median_multiple"),
                    _dec(s.get("realized_pnl_usd")),
                    s.get("avg_hold_minutes"),
                    s.get("distinct_tokens", 0),
                    s["score"],
                    s["is_smart"],
                )
                for s in scores
            ],
        )


async def top_wallets(chain_id: int, limit: int) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT wallet, score, win_rate, median_multiple, realized_pnl_usd, closed_trades
        FROM wallet_scores WHERE chain_id = $1 AND is_smart ORDER BY score DESC LIMIT $2
        """,
        chain_id,
        limit,
    )
    return [dict(r) for r in rows]


# --- signals -----------------------------------------------------------


async def insert_signal(payload: dict[str, Any]) -> int | None:
    """Insert a signal; returns None when the dedupe key already exists."""
    return await db.fetchval(
        """
        INSERT INTO signals (chain_id, token, ts, block_number, dedupe_key, distinct_wallets,
            distinct_clusters, wallets, native_spent, usd_spent, snap_price_usd, snap_mcap_usd,
            snap_fdv_usd, snap_liquidity_usd, snap_age_minutes, snap_holders, snap_buy_tax_bps,
            snap_sell_tax_bps, snap_top10_pct, snap_lp_locked_pct, snap_volume_24h_usd,
            safety_flags, safety_verdict, quality_score, quality_p2x)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,
                $22,$23,$24,$25)
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        payload["chain_id"],
        payload["token"].lower(),
        payload["ts"],
        payload.get("block_number"),
        payload["dedupe_key"],
        payload["distinct_wallets"],
        payload["distinct_clusters"],
        orjson.dumps(payload.get("wallets", [])).decode(),
        _dec(payload.get("native_spent")),
        _dec(payload.get("usd_spent")),
        _dec(payload.get("snap_price_usd")),
        _dec(payload.get("snap_mcap_usd")),
        _dec(payload.get("snap_fdv_usd")),
        _dec(payload.get("snap_liquidity_usd")),
        payload.get("snap_age_minutes"),
        payload.get("snap_holders"),
        payload.get("snap_buy_tax_bps"),
        payload.get("snap_sell_tax_bps"),
        payload.get("snap_top10_pct"),
        payload.get("snap_lp_locked_pct"),
        _dec(payload.get("snap_volume_24h_usd")),
        orjson.dumps(payload.get("safety_flags", [])).decode(),
        payload.get("safety_verdict", "unknown"),
        payload.get("quality_score"),
        payload.get("quality_p2x"),
    )


async def last_signal_at(chain_id: int, token: str) -> datetime | None:
    return await db.fetchval(
        "SELECT max(ts) FROM signals WHERE chain_id = $1 AND token = $2", chain_id, token.lower()
    )


async def signals_for_backtest(days: int) -> list[dict[str, Any]]:
    """Signals joined with their outcomes - the only input a backtest reads."""
    rows = await db.fetch(
        """
        SELECT s.id, s.chain_id, s.token, s.ts, s.distinct_wallets, s.distinct_clusters,
               s.snap_price_usd, s.snap_mcap_usd, s.snap_liquidity_usd, s.snap_age_minutes,
               s.snap_buy_tax_bps, s.snap_sell_tax_bps, s.safety_verdict, s.quality_score,
               s.usd_spent, t.symbol,
               o.peak_multiple, o.current_multiple, o.is_dead,
               o.mult_15m, o.mult_1h, o.mult_4h, o.mult_24h, o.mult_7d
        FROM signals s
        LEFT JOIN signal_outcomes o ON o.signal_id = s.id
        LEFT JOIN tokens t ON t.chain_id = s.chain_id AND t.address = s.token
        WHERE s.ts >= $1
        ORDER BY s.ts
        """,
        datetime.now(UTC) - timedelta(days=days),
    )
    return [dict(r) for r in rows]


async def mark_alert_sent(signal_id: int) -> None:
    await db.execute("UPDATE signals SET alert_sent_at = now() WHERE id = $1", signal_id)


async def pending_alert_signals(limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT s.*, t.symbol, t.name, t.pair_address, t.dex_id
        FROM signals s
        LEFT JOIN tokens t ON t.chain_id = s.chain_id AND t.address = s.token
        WHERE s.alert_sent_at IS NULL
        ORDER BY s.ts
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]
