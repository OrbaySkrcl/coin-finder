"""Alert dispatch: per-recipient pricing and the cost ceiling.

Uses a stub bot so the routing logic is tested without Telegram.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import asyncpg
import pytest

from coinfinder import db, repo
from coinfinder.bot.main import dispatch_alerts
from coinfinder.chains import BASE, BSC

DSN = (
    os.environ.get("COINFINDER_TEST_DSN")
    or os.environ.get("DATABASE_URL")
    or "postgresql://coinfinder@127.0.0.1:5432/coinfinder"
)
TABLES = (
    "alerts_sent, signal_outcomes, signals, paper_positions, wallet_trades, "
    "wallet_positions, wallet_clusters, wallet_scores, token_market, price_history, "
    "tokens, wallets, user_filters, users, ingest_checkpoints"
)
_MIGRATED = False


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(DSN, timeout=3)
    except Exception:
        return False
    await conn.close()
    return True


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


async def allow_thin_pools(telegram_id: int) -> None:
    """Drop the default 5k liquidity floor.

    Several cases below deliberately use thin pools to exercise the cost
    ceiling; without this they would be filtered in SQL first and the test
    would prove nothing about pricing.
    """
    await repo.update_filter(telegram_id, "min_liquidity_usd", 0)


class StubBot:
    """Records what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


async def make_signal(*, chain=BASE, liquidity=40_000.0, dedupe="s1") -> int:
    signal_id = await repo.insert_signal(
        {
            "chain_id": chain.chain_id,
            "token": "0xtok",
            "ts": datetime.now(UTC),
            "dedupe_key": dedupe,
            "distinct_wallets": 4,
            "distinct_clusters": 3,
            "wallets": ["0xa", "0xb", "0xc"],
            "usd_spent": 900.0,
            "snap_price_usd": 0.001,
            "snap_mcap_usd": 190_000.0,
            "snap_liquidity_usd": liquidity,
            "snap_age_minutes": 110,
            "safety_flags": [],
            "safety_verdict": "safe",
            "quality_score": 40.0,
            "quality_p2x": 0.34,
        }
    )
    assert signal_id is not None
    return signal_id


async def test_each_recipient_gets_the_cost_at_their_own_size():
    await repo.ensure_user(1, "small")
    await repo.ensure_user(2, "large")
    await repo.update_filter(1, "trade_size_usd", 10.0)
    await repo.update_filter(2, "trade_size_usd", 500.0)
    await make_signal()

    bot = StubBot()
    assert await dispatch_alerts(bot) == 2

    by_user = dict(bot.sent)
    assert "$10 için gidiş-dönüş" in by_user[1]
    assert "$500 için gidiş-dönüş" in by_user[2]


async def test_cost_ceiling_suppresses_an_uneconomic_signal():
    # A $500 position in a $3k pool costs far more than 2% to round-trip.
    await repo.ensure_user(1, "capped")
    await repo.update_filter(1, "trade_size_usd", 500.0)
    await repo.update_filter(1, "max_cost_pct", 2.0)
    await allow_thin_pools(1)
    await make_signal(liquidity=3_000.0)

    bot = StubBot()
    assert await dispatch_alerts(bot) == 0
    assert bot.sent == []


async def test_the_same_signal_passes_the_ceiling_at_a_smaller_size():
    await repo.ensure_user(1, "small")
    await repo.update_filter(1, "trade_size_usd", 10.0)
    await repo.update_filter(1, "max_cost_pct", 5.0)
    await allow_thin_pools(1)
    await make_signal(liquidity=3_000.0)

    bot = StubBot()
    assert await dispatch_alerts(bot) == 1


async def test_ceiling_is_chain_aware_at_small_sizes():
    # $5 on BNB Chain is mostly gas and breaches a 2% ceiling; the identical
    # signal on Base does not.
    await repo.ensure_user(1, "bnb")
    await repo.update_filter(1, "trade_size_usd", 5.0)
    await repo.update_filter(1, "max_cost_pct", 2.0)
    await repo.update_filter(1, "chains", ["base", "bsc"])

    await make_signal(chain=BSC, dedupe="bnb")
    bot = StubBot()
    assert await dispatch_alerts(bot) == 0

    await make_signal(chain=BASE, dedupe="base")
    bot = StubBot()
    assert await dispatch_alerts(bot) == 1


async def test_a_suppressed_signal_is_not_reconsidered():
    await repo.ensure_user(1, "capped")
    await repo.update_filter(1, "trade_size_usd", 500.0)
    await repo.update_filter(1, "max_cost_pct", 1.0)
    await allow_thin_pools(1)
    signal_id = await make_signal(liquidity=3_000.0)

    await dispatch_alerts(StubBot())
    seen = await db.fetchval(
        "SELECT count(*) FROM alerts_sent WHERE telegram_id = 1 AND signal_id = $1", signal_id
    )
    assert seen == 1


async def test_no_ceiling_means_everything_passes():
    await repo.ensure_user(1, "open")
    await repo.update_filter(1, "trade_size_usd", 500.0)
    await allow_thin_pools(1)
    await make_signal(liquidity=1_000.0)

    bot = StubBot()
    assert await dispatch_alerts(bot) == 1


async def test_signals_are_marked_sent_even_with_no_recipients():
    await make_signal()
    bot = StubBot()
    assert await dispatch_alerts(bot) == 0
    assert await repo.pending_alert_signals() == []


async def test_the_default_liquidity_floor_still_applies():
    # The cost ceiling is an extra gate, not a replacement: a pool below the
    # user's liquidity floor never reaches the pricing step at all.
    await repo.ensure_user(1, "default")
    await make_signal(liquidity=1_000.0)
    bot = StubBot()
    assert await dispatch_alerts(bot) == 0
