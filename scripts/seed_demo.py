"""Seed the database with a synthetic but realistically-shaped dataset.

Useful for exercising the dashboard and the API before real signals exist.
Everything written here is fake; the script refuses to run unless you pass
--yes so it can never be mistaken for production data.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "src")

from coinfinder import db, repo
from coinfinder.chains import ALL_CHAINS
from coinfinder.ingest.outcomes import OutcomeUpdate, persist
from coinfinder.logging_setup import setup_logging

HORIZON_HOURS = (("15m", 0.25), ("1h", 1), ("4h", 4), ("24h", 24), ("7d", 168))


class Lcg:
    def __init__(self, seed: int) -> None:
        self.s = seed

    def f(self) -> float:
        self.s = (1103515245 * self.s + 12345) & 0x7FFFFFFF
        return self.s / 0x7FFFFFFF

    def pick(self, items):
        return items[int(self.f() * len(items)) % len(items)]


async def seed(signals: int, days: int) -> None:
    rng = Lcg(2026)
    now = datetime.now(UTC)
    chains = list(ALL_CHAINS.values())

    # --- wallets and scores --------------------------------------------
    for chain in chains:
        wallets = [f"0x{chain.chain_id:04x}{i:036x}" for i in range(40)]
        await repo.upsert_wallets(chain.chain_id, wallets)
        await repo.set_watchlist(chain.chain_id, wallets[:30])
        await repo.upsert_scores(
            chain.chain_id,
            [
                {
                    "wallet": w,
                    "window_days": 90,
                    "closed_trades": 10 + int(rng.f() * 60),
                    "wins": 5 + int(rng.f() * 20),
                    "win_rate": 0.3 + rng.f() * 0.35,
                    "median_multiple": 1.0 + rng.f() * 1.8,
                    "realized_pnl_usd": rng.f() * 90_000,
                    "avg_hold_minutes": 30 + rng.f() * 900,
                    "distinct_tokens": 8 + int(rng.f() * 40),
                    "score": round(30 + rng.f() * 60, 2),
                    "is_smart": True,
                }
                for w in wallets[:30]
            ],
        )
        # A sybil cluster, so the dashboard shows collapse actually happening.
        await repo.replace_clusters(
            chain.chain_id,
            {wallets[0]: wallets[0], wallets[1]: wallets[0], wallets[2]: wallets[0]},
            "cobuy_timing",
        )

    # --- signals and outcomes ------------------------------------------
    written = 0
    for i in range(signals):
        chain = rng.pick(chains)
        ts = now - timedelta(days=days * rng.f())
        liq = rng.pick([2_500.0, 12_000.0, 45_000.0, 160_000.0])
        mcap = 25_000 + rng.f() * 800_000
        clusters = 3 + int(rng.f() * 4)
        verdict = rng.pick(["safe", "safe", "caution", "caution", "unknown"])
        if not chain.risk_data_available:
            verdict = "caution"

        roll = rng.f()
        if roll < 0.60:
            peak, current, dead = 0.3 + rng.f() * 0.7, 0.05 + rng.f() * 0.3, rng.f() < 0.4
        elif roll < 0.83:
            peak, current, dead = 1.0 + rng.f(), 0.5 + rng.f() * 0.8, False
        elif roll < 0.95:
            peak, current, dead = 2.0 + rng.f() * 3, 0.8 + rng.f() * 2, False
        else:
            peak, current, dead = 5.0 + rng.f() * 60, 1.0 + rng.f() * 12, False

        token = f"0x{i:040x}"
        await repo.upsert_tokens(
            chain.chain_id,
            [{"address": token, "symbol": f"DEMO{i}", "name": f"Demo Token {i}"}],
        )
        signal_id = await repo.insert_signal(
            {
                "chain_id": chain.chain_id,
                "token": token,
                "ts": ts,
                "dedupe_key": f"demo-{i}",
                "distinct_wallets": clusters + int(rng.f() * 3),
                "distinct_clusters": clusters,
                "wallets": [f"0x{chain.chain_id:04x}{j:036x}" for j in range(clusters)],
                "usd_spent": 200 + rng.f() * 4000,
                "snap_price_usd": 0.0001 + rng.f() * 0.01,
                "snap_mcap_usd": mcap,
                "snap_fdv_usd": mcap,
                "snap_liquidity_usd": liq,
                "snap_age_minutes": int(rng.f() * 4000),
                "snap_volume_24h_usd": rng.f() * 200_000,
                "safety_flags": [] if verdict == "safe" else ["lp_not_burned"],
                "safety_verdict": verdict,
                "quality_score": round(rng.f() * 100, 1),
                "quality_p2x": round(rng.f(), 3),
            }
        )
        if signal_id is None:
            continue
        age_hours = (now - ts).total_seconds() / 3600
        await persist(
            [
                OutcomeUpdate(
                    signal_id=signal_id,
                    last_price_usd=0.001 * current,
                    current_multiple=0.0 if dead else current,
                    peak_multiple=peak,
                    is_dead=dead,
                    horizons={
                        h: min(peak, 1.0 + rng.f() * (peak - 1.0 if peak > 1 else 0.2))
                        for h, hours in (
                            ("15m", 0.25),
                            ("1h", 1),
                            ("4h", 4),
                            ("24h", 24),
                            ("7d", 168),
                        )
                        if age_hours >= hours
                    },
                )
            ]
        )
        written += 1

    # --- a demo user ----------------------------------------------------
    await repo.ensure_user(1, "demo")
    print(f"seeded {written} signals across {len(chains)} chains")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", type=int, default=1200)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--yes", action="store_true", help="required: writes fake data")
    args = parser.parse_args()
    if not args.yes:
        raise SystemExit("refusing to write synthetic data without --yes")

    setup_logging()
    await db.init_pool(max_size=4)
    await db.migrate()
    try:
        await seed(args.signals, args.days)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
