"""Integration tests against a real PostgreSQL instance.

Every SQL statement in repo.py runs here. Skipped when no database is
reachable, so the unit suite stays runnable anywhere.

Set COINFINDER_TEST_DSN (or DATABASE_URL) to point at a throwaway database.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from coinfinder import db, repo
from coinfinder.chains import BASE
from coinfinder.ingest.extract import RawTrade

DSN = (
    os.environ.get("COINFINDER_TEST_DSN")
    or os.environ.get("DATABASE_URL")
    or ("postgresql://coinfinder@127.0.0.1:5432/coinfinder")
)
CHAIN = BASE.chain_id
NOW = datetime.now(UTC)


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(DSN, timeout=3)
    except Exception:
        return False
    await conn.close()
    return True


# asyncpg connections are bound to the event loop that created them, and
# pytest-asyncio gives each test its own loop, so the pool is per-test.
# Migrations only need to run once per session.
_MIGRATED = False

TABLES = (
    "alerts_sent, signal_outcomes, signals, paper_positions, wallet_trades, "
    "wallet_positions, wallet_clusters, wallet_scores, token_market, price_history, "
    "tokens, wallets, user_filters, users, ingest_checkpoints"
)


@pytest.fixture(autouse=True)
async def database():
    global _MIGRATED
    if not await _reachable():
        pytest.skip(f"no PostgreSQL at {DSN}")
    os.environ["DATABASE_URL"] = DSN
    from coinfinder.config import get_settings

    get_settings.cache_clear()
    await db.init_pool(min_size=1, max_size=4)
    if not _MIGRATED:
        await db.migrate()
        _MIGRATED = True
    async with db.acquire() as conn:
        await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    yield
    await db.close_pool()


# --- checkpoints -------------------------------------------------------


async def test_checkpoint_roundtrip():
    assert await repo.get_checkpoint(CHAIN, "wallet_watch") == 0
    await repo.set_checkpoint(CHAIN, "wallet_watch", 1234)
    assert await repo.get_checkpoint(CHAIN, "wallet_watch") == 1234
    await repo.set_checkpoint(CHAIN, "wallet_watch", 5678)
    assert await repo.get_checkpoint(CHAIN, "wallet_watch") == 5678


# --- wallets -----------------------------------------------------------


async def test_watchlist_lifecycle():
    await repo.upsert_wallets(CHAIN, ["0xAAA", "0xBBB", "0xCCC"])
    assert await repo.watched_wallets(CHAIN) == []  # inserted but not watched

    await repo.set_watchlist(CHAIN, ["0xAAA", "0xBBB"])
    assert await repo.watched_wallets(CHAIN) == ["0xaaa", "0xbbb"]

    # Replacing the list must drop wallets that fell out of the ranking.
    await repo.set_watchlist(CHAIN, ["0xBBB", "0xCCC"])
    assert await repo.watched_wallets(CHAIN) == ["0xbbb", "0xccc"]


async def test_contracts_are_excluded_from_the_watchlist():
    await repo.set_watchlist(CHAIN, ["0xAAA", "0xROUTER".lower()])
    await repo.mark_contracts(CHAIN, ["0xrouter"])
    assert await repo.watched_wallets(CHAIN) == ["0xaaa"]


async def test_exclusion_reason_is_recorded():
    await repo.set_watchlist(CHAIN, ["0xAAA"])
    await repo.exclude_wallets(CHAIN, ["0xAAA"], "bot_like_hold")
    assert await repo.watched_wallets(CHAIN) == []
    reason = await db.fetchval(
        "SELECT exclude_reason FROM wallets WHERE chain_id=$1 AND address=$2", CHAIN, "0xaaa"
    )
    assert reason == "bot_like_hold"


# --- trades ------------------------------------------------------------


def trade(tx, wallet="0xw1", token="0xt1", side="buy", native=0.5, minutes_ago=5, log_index=0):
    return RawTrade(
        wallet=wallet,
        token=token,
        side=side,
        token_amount=10**20,
        native_amount=native,
        tx_hash=tx,
        log_index=log_index,
        block_number=100,
        ts=NOW - timedelta(minutes=minutes_ago),
    )


async def test_insert_trades_is_idempotent():
    trades = [trade("0xtx1"), trade("0xtx2", wallet="0xw2")]
    await repo.insert_trades(CHAIN, trades, 3000.0)
    await repo.insert_trades(CHAIN, trades, 3000.0)  # replay
    count = await db.fetchval("SELECT count(*) FROM wallet_trades")
    assert count == 2


async def test_one_wallet_trading_repeatedly_in_one_batch():
    # Regression: ON CONFLICT DO UPDATE raises CardinalityViolation when a
    # single statement proposes the same key twice. One wallet making several
    # trades inside one scan window is the normal case, so this used to take
    # down every ingestion write.
    trades = [
        trade("0xtx1", wallet="0xw1", log_index=0),
        trade("0xtx1", wallet="0xw1", log_index=1),
        trade("0xtx2", wallet="0xw1", token="0xt2"),
    ]
    written = await repo.insert_trades(CHAIN, trades, 3000.0)
    assert written == 3
    assert await db.fetchval("SELECT count(*) FROM wallets") == 1
    assert await db.fetchval("SELECT count(*) FROM wallet_trades") == 3


async def test_set_watchlist_tolerates_duplicate_addresses():
    await repo.set_watchlist(CHAIN, ["0xAAA", "0xaaa", "0xBBB"])
    assert await repo.watched_wallets(CHAIN) == ["0xaaa", "0xbbb"]


async def test_insert_trades_creates_token_and_wallet_rows():
    await repo.insert_trades(CHAIN, [trade("0xtx1")], 3000.0)
    assert await db.fetchval("SELECT count(*) FROM tokens") == 1
    assert await db.fetchval("SELECT count(*) FROM wallets") == 1


async def test_usd_value_is_derived_from_the_native_leg():
    await repo.insert_trades(CHAIN, [trade("0xtx1", native=0.4)], 3000.0)
    value = await db.fetchval("SELECT usd_value FROM wallet_trades")
    assert float(value) == pytest.approx(1200.0)


async def test_recent_buys_only_returns_watched_wallets():
    await repo.insert_trades(
        CHAIN, [trade("0xtx1", wallet="0xw1"), trade("0xtx2", wallet="0xw2")], 3000.0
    )
    await repo.set_watchlist(CHAIN, ["0xw1"])
    buys = await repo.recent_buys(CHAIN, NOW - timedelta(hours=1))
    assert [b["wallet"] for b in buys] == ["0xw1"]


async def test_recent_buys_excludes_sells_and_old_trades():
    await repo.insert_trades(
        CHAIN,
        [
            trade("0xtx1", side="sell"),
            trade("0xtx2", minutes_ago=6000, log_index=1),
            trade("0xtx3", minutes_ago=1, log_index=2),
        ],
        3000.0,
    )
    await repo.set_watchlist(CHAIN, ["0xw1"])
    buys = await repo.recent_buys(CHAIN, NOW - timedelta(hours=1))
    assert len(buys) == 1


# --- clusters and scores -----------------------------------------------


async def test_clusters_and_scores_roundtrip():
    await repo.upsert_wallets(CHAIN, ["0xw1", "0xw2"])
    await repo.replace_clusters(CHAIN, {"0xw1": "0xw1", "0xw2": "0xw1"}, "cobuy_timing")
    assert await repo.cluster_map(CHAIN) == {"0xw1": "0xw1", "0xw2": "0xw1"}

    await repo.upsert_scores(
        CHAIN,
        [
            {
                "wallet": "0xw1",
                "window_days": 90,
                "closed_trades": 20,
                "wins": 12,
                "win_rate": 0.6,
                "median_multiple": 1.8,
                "realized_pnl_usd": 5000.0,
                "avg_hold_minutes": 120.0,
                "distinct_tokens": 15,
                "score": 71.5,
                "is_smart": True,
            }
        ],
    )
    top = await repo.top_wallets(CHAIN, 5)
    assert len(top) == 1 and float(top[0]["score"]) == pytest.approx(71.5)


# --- signals -----------------------------------------------------------


def signal_payload(dedupe="k1", clusters=3, **over):
    payload = {
        "chain_id": CHAIN,
        "token": "0xTOK",
        "ts": NOW,
        "dedupe_key": dedupe,
        "distinct_wallets": clusters,
        "distinct_clusters": clusters,
        "wallets": ["0xw1", "0xw2", "0xw3"],
        "usd_spent": 900.0,
        "snap_price_usd": 0.001,
        "snap_mcap_usd": 190_000.0,
        "snap_liquidity_usd": 40_000.0,
        "snap_age_minutes": 110,
        "safety_flags": ["lp_not_burned"],
        "safety_verdict": "caution",
        "quality_score": 34.0,
        "quality_p2x": 0.34,
    }
    payload.update(over)
    return payload


async def test_signal_dedupe_key_blocks_a_repeat():
    first = await repo.insert_signal(signal_payload())
    second = await repo.insert_signal(signal_payload())
    assert first is not None and second is None


async def test_rising_conviction_inserts_a_second_signal():
    await repo.insert_signal(signal_payload(dedupe="k1", clusters=3))
    again = await repo.insert_signal(signal_payload(dedupe="k2", clusters=5))
    assert again is not None


async def test_signal_snapshot_survives_the_roundtrip():
    signal_id = await repo.insert_signal(signal_payload())
    row = await db.fetchrow("SELECT * FROM signals WHERE id = $1", signal_id)
    assert float(row["snap_liquidity_usd"]) == pytest.approx(40_000.0)
    assert row["safety_verdict"] == "caution"
    assert row["token"] == "0xtok"  # lower-cased on write


async def test_pending_alerts_then_marked_sent():
    signal_id = await repo.insert_signal(signal_payload())
    assert len(await repo.pending_alert_signals()) == 1
    await repo.mark_alert_sent(signal_id)
    assert await repo.pending_alert_signals() == []


# --- users and alert routing -------------------------------------------


async def test_ensure_user_is_idempotent_and_creates_filters():
    a = await repo.ensure_user(1, "orbay")
    b = await repo.ensure_user(1, "orbay")
    assert a["telegram_id"] == b["telegram_id"] == 1
    assert a["min_clusters"] == 3
    assert await db.fetchval("SELECT count(*) FROM user_filters") == 1


async def test_recipients_match_on_filters():
    await repo.ensure_user(1, "matches")
    await repo.ensure_user(2, "too_strict")
    await repo.update_filter(2, "min_clusters", 9)
    signal_id = await repo.insert_signal(signal_payload())
    signal = (await repo.pending_alert_signals())[0]
    recipients = await repo.recipients_for(signal, "base")
    assert recipients == [1]
    _ = signal_id


async def test_recipients_respect_chain_selection():
    await repo.ensure_user(1, "bsc_only")
    await repo.update_filter(1, "chains", ["bsc"])
    await repo.insert_signal(signal_payload())
    signal = (await repo.pending_alert_signals())[0]
    assert await repo.recipients_for(signal, "base") == []


async def test_require_safe_blocks_danger_signals():
    await repo.ensure_user(1, "careful")
    await repo.insert_signal(signal_payload(safety_verdict="danger"))
    signal = (await repo.pending_alert_signals())[0]
    assert await repo.recipients_for(signal, "base") == []


async def test_paused_and_blocked_users_receive_nothing():
    await repo.ensure_user(1, "paused")
    await repo.ensure_user(2, "blocked")
    await repo.set_alerts_paused(1, True)
    await repo.block_user(2)
    await repo.insert_signal(signal_payload())
    signal = (await repo.pending_alert_signals())[0]
    assert await repo.recipients_for(signal, "base") == []


async def test_expired_trial_receives_nothing():
    await repo.ensure_user(1, "expired")
    await db.execute(
        "UPDATE users SET trial_ends_at = now() - interval '1 day' WHERE telegram_id = 1"
    )
    await repo.insert_signal(signal_payload())
    signal = (await repo.pending_alert_signals())[0]
    assert await repo.recipients_for(signal, "base") == []


async def test_a_signal_is_never_sent_to_the_same_user_twice():
    await repo.ensure_user(1, "once")
    signal_id = await repo.insert_signal(signal_payload())
    signal = (await repo.pending_alert_signals())[0]
    assert await repo.recipients_for(signal, "base") == [1]
    await repo.record_alert(1, signal_id)
    assert await repo.recipients_for(signal, "base") == []


async def test_update_filter_rejects_unknown_columns():
    await repo.ensure_user(1, "x")
    with pytest.raises(ValueError, match="not a filter field"):
        await repo.update_filter(1, "chains); DROP TABLE users; --", "boom")


# --- backtest feed -----------------------------------------------------


async def test_signals_for_backtest_joins_outcomes_and_symbols():
    signal_id = await repo.insert_signal(signal_payload())
    await repo.upsert_tokens(CHAIN, [{"address": "0xtok", "symbol": "COLLECT"}])
    from coinfinder.ingest.outcomes import OutcomeUpdate, persist

    await persist(
        [
            OutcomeUpdate(
                signal_id=signal_id,
                last_price_usd=0.003,
                current_multiple=3.0,
                peak_multiple=5.0,
                is_dead=False,
                horizons={"1h": 2.0},
            )
        ]
    )
    rows = await repo.signals_for_backtest(30)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "COLLECT"
    assert float(rows[0]["peak_multiple"]) == pytest.approx(5.0)
    assert float(rows[0]["mult_1h"]) == pytest.approx(2.0)


async def test_outcome_horizons_are_written_once():
    from coinfinder.ingest.outcomes import OutcomeUpdate, persist

    signal_id = await repo.insert_signal(signal_payload())
    await persist([OutcomeUpdate(signal_id, 0.002, 2.0, 2.0, False, {"1h": 2.0})])
    # A later refresh must not overwrite what the 1-hour stop would have got.
    await persist([OutcomeUpdate(signal_id, 0.0001, 0.1, 2.0, False, {"1h": 0.1})])
    row = await db.fetchrow("SELECT * FROM signal_outcomes WHERE signal_id = $1", signal_id)
    assert float(row["mult_1h"]) == pytest.approx(2.0)
    assert float(row["current_multiple"]) == pytest.approx(0.1)


async def test_peak_never_decreases_in_the_database():
    from coinfinder.ingest.outcomes import OutcomeUpdate, persist

    signal_id = await repo.insert_signal(signal_payload())
    await persist([OutcomeUpdate(signal_id, 0.01, 10.0, 10.0, False, {})])
    await persist([OutcomeUpdate(signal_id, 0.0001, 0.1, 0.1, False, {})])
    peak = await db.fetchval(
        "SELECT peak_multiple FROM signal_outcomes WHERE signal_id = $1", signal_id
    )
    assert float(peak) == pytest.approx(10.0)


async def test_end_to_end_backtest_over_database_rows():
    from coinfinder.backtest.engine import FilterSpec, run
    from coinfinder.backtest.exits import by_name
    from coinfinder.ingest.outcomes import OutcomeUpdate, persist

    for i in range(40):
        signal_id = await repo.insert_signal(
            signal_payload(dedupe=f"k{i}", token=f"0xtok{i}", clusters=3 + i % 3)
        )
        await persist(
            [
                OutcomeUpdate(
                    signal_id=signal_id,
                    last_price_usd=0.003,
                    current_multiple=0.5 if i % 2 else 3.0,
                    peak_multiple=1.0 if i % 2 else 6.0,
                    is_dead=bool(i % 4 == 1),
                    horizons={"1h": 1.2},
                )
            ]
        )

    rows = await repo.signals_for_backtest(30)
    assert len(rows) == 40
    result = run(rows, spec=FilterSpec(min_clusters=3), exit_model=by_name("tp_2x"))
    assert result.signals == 40
    assert result.win_rate is not None
    assert result.dead_share == pytest.approx(0.25)
    assert result.median_round_trip_cost_pct is not None
