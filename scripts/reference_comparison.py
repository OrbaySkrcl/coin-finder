"""Show what execution costs and exit choice do to a published backtest.

The signal population here is SYNTHETIC. It is calibrated to the outcome
distribution the reference product publishes in its own screenshots (3,051
signals, 39.9% win rate, 1.54x median, and the flat/1-2x/2-5x/5-10x/10x+ bar
chart), so the shape is theirs while the individual tokens are made up.

This is therefore a demonstration of what our engine reports given that
distribution - not a measurement of anyone's bot.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "src")

from coinfinder.backtest.costs import CostModel
from coinfinder.backtest.engine import FilterSpec, run
from coinfinder.backtest.exits import DEFAULT_MODELS, by_name

T0 = datetime(2026, 6, 1, tzinfo=UTC)

# Published bucket counts (read off the reference product's distribution chart).
BUCKETS = [
    ("flat", 1835, 0.0, 1.0),
    ("1-2x", 700, 1.0, 2.0),
    ("2-5x", 250, 2.0, 5.0),
    ("5-10x", 136, 5.0, 10.0),
    ("10x+", 130, 10.0, 120.0),
]

#: Tuned so hold-to-now lands on the published 0.26x median / 18.8% win rate.
RETENTION_EXPONENT = 1.15

# Liquidity mix typical of the lowcaps these signals cover.
LIQUIDITY_TIERS = [(3_000.0, 0.25), (15_000.0, 0.35), (40_000.0, 0.25), (150_000.0, 0.15)]


class Lcg:
    def __init__(self, seed: int) -> None:
        self.s = seed

    def next(self) -> float:
        self.s = (1103515245 * self.s + 12345) & 0x7FFFFFFF
        return self.s / 0x7FFFFFFF


def build_population() -> list[dict]:
    rng = Lcg(42)
    rows: list[dict] = []
    idx = 0
    for _, count, lo, hi in BUCKETS:
        for _ in range(count):
            peak = lo + (hi - lo) * rng.next()
            # Give-back is calibrated so that holding to now reproduces the
            # reference product's own "reality check" panel: ~0.26x median and
            # ~19% of tokens still above 1x.
            current = peak * (rng.next() ** RETENTION_EXPONENT)
            dead = peak < 1.0 and rng.next() < 0.55

            roll, liq = rng.next(), LIQUIDITY_TIERS[-1][0]
            acc = 0.0
            for tier, weight in LIQUIDITY_TIERS:
                acc += weight
                if roll <= acc:
                    liq = tier
                    break

            rows.append(
                {
                    "id": idx,
                    "chain_id": 8453,
                    "token": f"0x{idx:040x}",
                    "symbol": f"TKN{idx}",
                    "ts": None,  # assigned after shuffling
                    "distinct_clusters": 3 + int(rng.next() * 4),
                    "snap_mcap_usd": 30_000 + rng.next() * 400_000,
                    "snap_liquidity_usd": liq,
                    "snap_price_usd": 0.0001,
                    "snap_age_minutes": int(rng.next() * 3000),
                    "safety_verdict": "safe" if rng.next() > 0.3 else "caution",
                    "quality_score": 30 + rng.next() * 60,
                    "peak_multiple": peak,
                    "current_multiple": 0.0 if dead else current,
                    "is_dead": dead,
                    "mult_15m": min(peak, 1.0 + rng.next() * 0.6),
                    "mult_1h": min(peak, 1.0 + rng.next() * 1.2),
                    "mult_4h": min(peak, 1.0 + rng.next() * 2.5),
                    "mult_24h": min(peak, current * (0.6 + rng.next())),
                    "mult_7d": current * (0.4 + rng.next()),
                }
            )
            idx += 1

    # Buckets are generated in order, so without a shuffle every loser would
    # sit at the start of the timeline and the out-of-sample split would just
    # be measuring that ordering.
    for i in range(len(rows) - 1, 0, -1):
        j = int(rng.next() * (i + 1))
        rows[i], rows[j] = rows[j], rows[i]
    for position, row in enumerate(rows):
        row["ts"] = T0 + timedelta(hours=position * 0.7)
    return rows


def main() -> None:
    rows = build_population()
    cost = CostModel(dex_fee_bps=30, gas_usd_per_swap=0.02)
    size = 100.0

    hold = run(rows, spec=FilterSpec(), exit_model=by_name("hold_to_now"), size_usd=size, cost=cost)
    print(f"Synthetic population: {len(rows):,} signals, ${size:.0f} per trade")
    print(
        "Calibration check vs the reference product's published reality-check panel: "
        f"hold-to-now median {hold.median_net_multiple:.2f}x (published 0.26x), "
        f"win rate {hold.win_rate:.1%} (published 18.8%)\n"
    )
    print(
        f"{'exit model':<14} {'hindsight':<10} {'win rate':>10} {'median':>9} "
        f"{'ROI':>10} {'PnL':>14}"
    )
    print("-" * 72)

    for model in DEFAULT_MODELS:
        r = run(rows, spec=FilterSpec(), exit_model=model, size_usd=size, cost=cost)
        flag = "YES" if r.uses_look_ahead else "-"
        print(
            f"{r.exit_model:<14} {flag:<10} {r.win_rate:>9.1%} {r.median_net_multiple:>8.3f}x "
            f"{r.roi_pct:>9.1f}% ${r.total_pnl_usd:>12,.0f}"
        )

    print("\nSame signals, same $100 size - the exit rule is the entire difference.")

    print("\n--- what position size does to the best realistic strategy ---")
    best = max(
        (m for m in DEFAULT_MODELS if not m.uses_look_ahead),
        key=lambda m: run(rows, spec=FilterSpec(), exit_model=m, size_usd=size, cost=cost).roi_pct,
    )
    print(f"exit model: {best.name}\n")
    print(f"{'size':>10} {'ROI':>10} {'median':>10} {'median round-trip cost':>26}")
    print("-" * 60)
    for trade_size in (50.0, 100.0, 500.0, 2_000.0, 10_000.0):
        r = run(rows, spec=FilterSpec(), exit_model=best, size_usd=trade_size, cost=cost)
        print(
            f"${trade_size:>9,.0f} {r.roi_pct:>9.1f}% {r.median_net_multiple:>9.3f}x "
            f"{r.median_round_trip_cost_pct:>25.2f}%"
        )

    print("\n--- out-of-sample check on the headline filter ---")
    r = run(
        rows,
        spec=FilterSpec(min_clusters=5, safety_verdicts=("safe",)),
        exit_model=best,
        size_usd=size,
        cost=cost,
        split_at=T0 + timedelta(days=60),
    )
    print(f"filter: {r.filter_label} | signals: {r.signals}")
    if r.win_rate_ci:
        print(f"win rate {r.win_rate:.1%}  95% CI [{r.win_rate_ci[0]:.1%}, {r.win_rate_ci[1]:.1%}]")
    if r.median_ci:
        print(
            f"median   {r.median_net_multiple:.3f}x  95% CI "
            f"[{r.median_ci[0]:.3f}x, {r.median_ci[1]:.3f}x]"
        )
    if r.out_of_sample:
        for half in ("in_sample", "out_of_sample"):
            s = r.out_of_sample[half]
            print(
                f"  {half:<15} n={s['signals']:<5} win={s['win_rate']:.1%} roi={s['roi_pct']:.1f}%"
            )


if __name__ == "__main__":
    main()
