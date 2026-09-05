from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coinfinder.backtest.costs import CostModel
from coinfinder.backtest.engine import (
    FilterSpec,
    apply_costs,
    apply_filters,
    bucket_counts,
    exit_expr,
    run,
    search,
    to_frame,
)
from coinfinder.backtest.exits import (
    DEFAULT_MODELS,
    FixedTakeProfit,
    HoldToNow,
    Ladder,
    PeakFraction,
    TimeStop,
    realistic_models,
)

T0 = datetime(2026, 7, 1, tzinfo=UTC)


def sig(
    i,
    *,
    peak=2.0,
    current=1.0,
    dead=False,
    liq=50_000.0,
    mcap=200_000.0,
    clusters=3,
    age=60,
    verdict="safe",
    chain=8453,
    quality=50.0,
    day=0,
):
    return {
        "id": i,
        "chain_id": chain,
        "token": f"0xtok{i}",
        "symbol": f"T{i}",
        "ts": T0 + timedelta(days=day, minutes=i),
        "distinct_clusters": clusters,
        "snap_mcap_usd": mcap,
        "snap_liquidity_usd": liq,
        "snap_price_usd": 0.001,
        "snap_age_minutes": age,
        "safety_verdict": verdict,
        "quality_score": quality,
        "peak_multiple": peak,
        "current_multiple": current,
        "is_dead": dead,
        "mult_15m": 1.1,
        "mult_1h": 1.4,
        "mult_4h": 1.8,
        "mult_24h": 0.9,
        "mult_7d": 0.5,
    }


# --- frame & filters ---------------------------------------------------


def test_empty_input_gives_typed_empty_frame():
    df = to_frame([])
    assert df.height == 0 and "net_multiple" not in df.columns


def test_filters_compose():
    rows = [
        sig(0, clusters=3, mcap=50_000),
        sig(1, clusters=6, mcap=50_000),
        sig(2, clusters=6, mcap=900_000),
    ]
    df = to_frame(rows)
    assert apply_filters(df, FilterSpec(min_clusters=5)).height == 2
    assert apply_filters(df, FilterSpec(min_clusters=5, max_mcap_usd=100_000)).height == 1
    assert apply_filters(df, FilterSpec(chains=(56,))).height == 0


def test_unknown_snapshot_values_do_not_pass_a_filter():
    # A token whose mcap was never captured is not an answer to "mcap < 100k".
    row = sig(0)
    row["snap_mcap_usd"] = None
    assert apply_filters(to_frame([row]), FilterSpec(max_mcap_usd=100_000)).height == 0


def test_safety_filter():
    rows = [sig(0, verdict="safe"), sig(1, verdict="caution"), sig(2, verdict="danger")]
    df = apply_filters(to_frame(rows), FilterSpec(safety_verdicts=("safe", "caution")))
    assert df.height == 2


# --- exit expressions match the scalar models --------------------------


@pytest.mark.parametrize("model", DEFAULT_MODELS, ids=lambda m: m.name)
def test_vectorised_exit_matches_scalar_model(model):
    from coinfinder.backtest.exits import Outcome

    rows = [
        sig(0, peak=8.0, current=0.4),
        sig(1, peak=1.2, current=1.1),
        sig(2, peak=1.0, current=0.0, dead=True),
        sig(3, peak=25.0, current=12.0),
    ]
    df = to_frame(rows).with_columns(gross_multiple=exit_expr(model))
    got = df["gross_multiple"].to_list()
    for row, value in zip(rows, got, strict=True):
        outcome = Outcome(
            peak_multiple=row["peak_multiple"],
            current_multiple=row["current_multiple"],
            is_dead=row["is_dead"],
            horizons={h: row[f"mult_{h}"] for h in ("15m", "1h", "4h", "24h", "7d")},
        )
        assert value == pytest.approx(model.gross_multiple(outcome), rel=1e-9)


def test_dead_token_with_no_price_path_is_zero_everywhere():
    row = sig(0, peak=1.5, current=None, dead=True)
    for horizon in ("15m", "1h", "4h", "24h", "7d"):
        row[f"mult_{horizon}"] = None
    df = to_frame([row])
    for model in realistic_models():
        value = df.with_columns(gross_multiple=exit_expr(model))["gross_multiple"][0]
        assert value == pytest.approx(0.0), model.name


def test_a_time_stop_keeps_a_gain_taken_before_the_token_died():
    # Exiting at 1.4x one hour in is a real, achievable outcome. The token
    # dying later does not retroactively take that away - which is exactly why
    # a time stop is classified as realistic rather than look-ahead.
    df = to_frame([sig(0, peak=1.5, current=None, dead=True)])  # mult_1h = 1.4
    assert df.with_columns(gross_multiple=exit_expr(TimeStop("1h")))["gross_multiple"][0] == (
        pytest.approx(1.4)
    )
    assert df.with_columns(gross_multiple=exit_expr(HoldToNow()))["gross_multiple"][0] == (
        pytest.approx(0.0)
    )


def test_look_ahead_models_inflate_a_rug():
    # The whole point: hindsight models pay out on a token that went to zero.
    df = to_frame([sig(0, peak=1.5, current=None, dead=True)])
    assert df.with_columns(gross_multiple=exit_expr(PeakFraction(0.5)))["gross_multiple"][0] > 0.0
    assert df.with_columns(gross_multiple=exit_expr(HoldToNow()))["gross_multiple"][0] == 0.0


# --- costs -------------------------------------------------------------


def test_costs_reduce_every_multiple():
    df = to_frame([sig(i, peak=5.0, current=3.0) for i in range(5)])
    scored = apply_costs(
        df.with_columns(gross_multiple=exit_expr(FixedTakeProfit(3.0))),
        size_usd=100.0,
        cost=CostModel(),
    )
    assert all(n < 3.0 for n in scored["net_multiple"].to_list())


def test_thin_pool_is_unexitable_in_the_frame():
    df = to_frame([sig(0, peak=10.0, current=8.0, liq=100.0)])
    scored = apply_costs(
        df.with_columns(gross_multiple=exit_expr(FixedTakeProfit(5.0))),
        size_usd=100.0,
        cost=CostModel(),
    )
    assert scored["net_multiple"][0] == pytest.approx(0.0)


def test_frame_costs_agree_with_scalar_cost_model():
    from coinfinder.backtest.costs import net_multiple

    cost = CostModel(dex_fee_bps=30, gas_usd_per_swap=0.02)
    df = to_frame([sig(0, peak=4.0, current=2.0, liq=40_778.0)])
    scored = apply_costs(
        df.with_columns(gross_multiple=exit_expr(FixedTakeProfit(3.0))), size_usd=100.0, cost=cost
    )
    expected = net_multiple(3.0, size_usd=100.0, entry_liquidity_usd=40_778.0, model=cost)
    assert scored["net_multiple"][0] == pytest.approx(expected, rel=1e-9)


# --- summaries ---------------------------------------------------------


def test_buckets_partition_the_sample():
    counts = bucket_counts([0.0, 0.5, 1.5, 3.0, 7.0, 40.0])
    assert counts == {"flat (<1x)": 2, "1-2x": 1, "2-5x": 1, "5-10x": 1, "10x+": 1}
    assert sum(counts.values()) == 6


def test_run_reports_no_signals_cleanly():
    r = run([], spec=FilterSpec(), exit_model=HoldToNow())
    assert r.signals == 0 and r.win_rate is None
    assert "No signals matched this filter." in r.warnings


def test_look_ahead_model_carries_a_warning():
    rows = [sig(i, peak=5.0, current=1.0) for i in range(40)]
    r = run(rows, spec=FilterSpec(), exit_model=PeakFraction(0.5))
    assert r.uses_look_ahead
    assert any("hindsight" in w for w in r.warnings)


def test_small_sample_carries_a_warning():
    r = run([sig(i) for i in range(5)], spec=FilterSpec(), exit_model=HoldToNow())
    assert any("Only 5 signals" in w for w in r.warnings)


def test_sample_size_warning_threshold():
    from coinfinder.backtest.engine import SMALL_SAMPLE_WARNING

    below = run(
        [sig(i) for i in range(SMALL_SAMPLE_WARNING - 1)],
        spec=FilterSpec(),
        exit_model=HoldToNow(),
    )
    at_or_above = run(
        [sig(i) for i in range(SMALL_SAMPLE_WARNING)], spec=FilterSpec(), exit_model=HoldToNow()
    )
    assert any("Only" in w for w in below.warnings)
    assert not any("Only" in w for w in at_or_above.warnings)


def test_bootstrap_interval_brackets_the_point_estimate():
    rows = [sig(i, peak=3.0 if i % 3 else 1.0, current=1.0) for i in range(120)]
    r = run(rows, spec=FilterSpec(), exit_model=FixedTakeProfit(2.0))
    assert r.win_rate_ci is not None and r.median_ci is not None
    lo, hi = r.win_rate_ci
    assert lo <= r.win_rate <= hi


def test_dead_tokens_are_counted_not_dropped():
    rows = [sig(i, dead=i < 60, peak=1.0, current=0.0) for i in range(100)]
    r = run(rows, spec=FilterSpec(), exit_model=HoldToNow())
    assert r.signals == 100  # survivorship: rugs stay in the sample
    assert r.dead_share == pytest.approx(0.6)
    assert r.total_pnl_usd < 0


def test_out_of_sample_split_reports_both_halves():
    rows = [sig(i, peak=5.0, current=2.0, day=0) for i in range(40)]
    rows += [sig(100 + i, peak=1.0, current=0.3, day=30) for i in range(40)]
    r = run(
        rows,
        spec=FilterSpec(),
        exit_model=FixedTakeProfit(2.0),
        split_at=T0 + timedelta(days=15),
    )
    assert r.out_of_sample is not None
    ins = r.out_of_sample["in_sample"]
    oos = r.out_of_sample["out_of_sample"]
    assert ins["signals"] == 40 and oos["signals"] == 40
    # A combo that looked great in-sample collapses out-of-sample.
    assert ins["roi_pct"] > 0 > oos["roi_pct"]


# --- search ------------------------------------------------------------


def test_search_excludes_look_ahead_models_by_default():
    rows = [sig(i, peak=10.0, current=1.0) for i in range(60)]
    results = search(rows, specs=[FilterSpec()], min_signals=10)
    assert results and all(not r.uses_look_ahead for r in results)
    with_la = search(rows, specs=[FilterSpec()], min_signals=10, include_look_ahead=True)
    assert any(r.uses_look_ahead for r in with_la)


def test_search_drops_combos_below_the_sample_floor():
    rows = [sig(i, clusters=3) for i in range(60)]
    results = search(rows, specs=[FilterSpec(min_clusters=99), FilterSpec()], min_signals=25)
    assert all(r.signals >= 25 for r in results)
    assert all(r.filter_label != "99w+ / any MC" for r in results)


def test_search_ranks_by_roi():
    rows = [sig(i, peak=6.0, current=3.0, clusters=6) for i in range(40)]
    rows += [sig(100 + i, peak=1.0, current=0.2, clusters=3) for i in range(40)]
    results = search(
        rows,
        specs=[FilterSpec(min_clusters=6), FilterSpec(min_clusters=3)],
        exit_models=(FixedTakeProfit(3.0),),
        min_signals=10,
    )
    assert results[0].filter_label.startswith("6w+")
    assert results[0].roi_pct > results[-1].roi_pct


def test_ladder_beats_hold_on_a_round_tripping_token():
    rows = [sig(i, peak=8.0, current=0.3) for i in range(50)]
    ladder = run(rows, spec=FilterSpec(), exit_model=Ladder())
    hold = run(rows, spec=FilterSpec(), exit_model=HoldToNow())
    assert ladder.total_pnl_usd > hold.total_pnl_usd
    assert hold.total_pnl_usd < 0


def test_time_stop_uses_the_horizon_column():
    rows = [sig(i, peak=99.0, current=0.1) for i in range(40)]
    r = run(rows, spec=FilterSpec(), exit_model=TimeStop("4h"))
    # mult_4h is 1.8 for every row, so the median lands just under it.
    assert 1.6 < r.median_net_multiple < 1.8


# --- ranking -----------------------------------------------------------


def test_shrunk_roi_penalises_small_samples():
    from coinfinder.backtest.engine import RANKING_PRIOR_SIGNALS

    small = run(
        [sig(i, peak=6.0, current=3.0) for i in range(30)],
        spec=FilterSpec(),
        exit_model=FixedTakeProfit(3.0),
    )
    large = run(
        [sig(i, peak=6.0, current=3.0) for i in range(600)],
        spec=FilterSpec(),
        exit_model=FixedTakeProfit(3.0),
    )
    # Identical per-signal outcomes, so raw ROI matches...
    assert small.roi_pct == pytest.approx(large.roi_pct, rel=1e-6)
    # ...but the larger sample carries far more of it into the ranking.
    assert large.shrunk_roi_pct > small.shrunk_roi_pct * 2
    weight = 30 / (30 + RANKING_PRIOR_SIGNALS)
    assert small.shrunk_roi_pct == pytest.approx(small.roi_pct * weight, rel=1e-6)


def test_search_prefers_a_large_solid_result_over_a_marginally_better_small_one():
    # The realistic trap: a 30-signal combination edges out a 400-signal one on
    # raw ROI by a modest margin. Shrinkage must hand first place to the one
    # with the evidence behind it.
    lucky = [sig(i, peak=5.0, current=4.2, clusters=9) for i in range(30)]
    solid = [sig(1000 + i, peak=4.0, current=3.4, clusters=3) for i in range(400)]
    results = search(
        lucky + solid,
        specs=[FilterSpec(min_clusters=9), FilterSpec(max_clusters=3)],
        exit_models=(HoldToNow(),),
        min_signals=25,
    )
    by_size = {r.signals: r for r in results}
    assert by_size[30].roi_pct > by_size[400].roi_pct  # the trap
    assert results[0].signals == 400  # ...which shrinkage avoids
    assert by_size[400].shrunk_roi_pct > by_size[30].shrunk_roi_pct


def test_shrinkage_does_not_bury_a_genuinely_large_edge():
    # Shrinkage is a penalty proportional to missing evidence, not a veto. A
    # small sample returning many times more must still be able to rank first,
    # otherwise the leaderboard just sorts by sample size.
    huge_edge = [sig(i, peak=20.0, current=15.0, clusters=9) for i in range(30)]
    ordinary = [sig(1000 + i, peak=4.0, current=3.0, clusters=3) for i in range(400)]
    results = search(
        huge_edge + ordinary,
        specs=[FilterSpec(min_clusters=9), FilterSpec(max_clusters=3)],
        exit_models=(HoldToNow(),),
        min_signals=25,
    )
    assert results[0].signals == 30
    assert any("Only 30 signals" in w for w in results[0].warnings)
    assert any("interval" in w for w in results[0].warnings)


def test_search_computes_intervals_only_for_the_results_it_returns():
    rows = [sig(i, peak=4.0, current=2.0, clusters=3 + i % 4) for i in range(400)]
    results = search(
        rows,
        specs=[FilterSpec(min_clusters=c) for c in (3, 4, 5, 6)],
        exit_models=(FixedTakeProfit(2.0), HoldToNow()),
        min_signals=10,
        top_n_with_intervals=2,
    )
    assert len(results) > 2
    assert all(r.win_rate_ci is not None for r in results[:2])
    assert all(r.win_rate_ci is None for r in results[2:])


def test_run_can_skip_intervals():
    rows = [sig(i) for i in range(100)]
    fast = run(rows, spec=FilterSpec(), exit_model=HoldToNow(), with_intervals=False)
    full = run(rows, spec=FilterSpec(), exit_model=HoldToNow(), with_intervals=True)
    assert fast.win_rate_ci is None and full.win_rate_ci is not None
    assert fast.win_rate == full.win_rate


# --- bootstrap ---------------------------------------------------------


def test_bootstrap_resamples_with_replacement_at_every_sample_size():
    """Regression for a silent zero-width confidence interval.

    A hand-rolled LCG using ``state % n`` produced a permutation rather than a
    resample whenever n was a power of two, so the bootstrap became a no-op and
    reported perfect certainty. Powers of two are exactly the sizes a synthetic
    dataset is likely to have, so this hid easily.
    """
    from coinfinder.backtest.engine import _bootstrap_ci

    for n in (64, 100, 512, 997, 1024):
        # Half the sample at 0.5x and half at 4x: any honest resample of this
        # has a genuinely uncertain median and win rate.
        values = [0.5 if i % 2 else 4.0 for i in range(n)]
        win_ci = _bootstrap_ci(values, "win_rate")
        assert win_ci is not None, n
        assert win_ci[1] > win_ci[0], f"zero-width win-rate interval at n={n}"
        assert win_ci[0] <= 0.5 <= win_ci[1], n


def test_bootstrap_interval_narrows_as_the_sample_grows():
    from coinfinder.backtest.engine import _bootstrap_ci

    def width(n: int) -> float:
        ci = _bootstrap_ci([0.5 if i % 2 else 4.0 for i in range(n)], "win_rate")
        assert ci is not None
        return ci[1] - ci[0]

    assert width(50) > width(500) > width(2000) > 0


def test_bootstrap_is_deterministic():
    from coinfinder.backtest.engine import _bootstrap_ci

    values = [0.5 if i % 3 else 3.0 for i in range(200)]
    assert _bootstrap_ci(values, "median") == _bootstrap_ci(values, "median")


def test_bootstrap_declines_on_tiny_samples():
    from coinfinder.backtest.engine import _bootstrap_ci

    assert _bootstrap_ci([1.0] * 11, "median") is None
    assert _bootstrap_ci([1.0] * 12, "median") is not None
