"""Strategy Lab backtest engine.

Reads only the immutable ``snap_*`` fields of a signal plus its recorded
outcome, so a backtest can never see information that did not exist when the
signal fired.

What it reports that the reference product does not:

* execution costs, so a median multiple is net of fees, slippage and gas;
* dead tokens carried at zero instead of dropping out of the sample;
* bootstrap confidence intervals, because a win rate from 40 trades is not a
  number, it is a range;
* an out-of-sample split, so a filter combination that merely fits the past is
  visible as such.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import polars as pl

from coinfinder.backtest.costs import MIN_USABLE_LIQUIDITY_USD, CostModel
from coinfinder.backtest.exits import (
    DEFAULT_MODELS,
    ExitModel,
    FixedTakeProfit,
    HoldToNow,
    Ladder,
    PeakFraction,
    TimeStop,
    TrailingStop,
)

BUCKETS = ("flat (<1x)", "1-2x", "2-5x", "5-10x", "10x+")


@dataclass(slots=True)
class FilterSpec:
    chains: tuple[int, ...] | None = None
    min_clusters: int | None = None
    max_clusters: int | None = None
    min_mcap_usd: float | None = None
    max_mcap_usd: float | None = None
    min_liquidity_usd: float | None = None
    max_liquidity_usd: float | None = None
    max_age_minutes: int | None = None
    safety_verdicts: tuple[str, ...] | None = None
    min_quality: float | None = None

    def label(self) -> str:
        parts: list[str] = []
        if self.min_clusters:
            parts.append(f"{self.min_clusters}w+")
        if self.min_mcap_usd or self.max_mcap_usd:
            lo = f"{int(self.min_mcap_usd / 1000)}k" if self.min_mcap_usd else "0"
            hi = f"{int(self.max_mcap_usd / 1000)}k" if self.max_mcap_usd else "inf"
            parts.append(f"MC {lo}-{hi}")
        else:
            parts.append("any MC")
        if self.min_liquidity_usd:
            parts.append(f"liq>{int(self.min_liquidity_usd / 1000)}k")
        if self.max_age_minutes:
            parts.append(f"age<{self.max_age_minutes}m")
        if self.safety_verdicts:
            parts.append("+".join(self.safety_verdicts))
        return " / ".join(parts) or "all signals"


@dataclass(slots=True)
class BacktestResult:
    filter_label: str
    exit_model: str
    uses_look_ahead: bool
    signals: int
    win_rate: float | None
    win_rate_ci: tuple[float, float] | None
    median_net_multiple: float | None
    median_ci: tuple[float, float] | None
    mean_net_multiple: float | None
    total_pnl_usd: float
    roi_pct: float | None
    invested_usd: float
    dead_share: float | None
    median_round_trip_cost_pct: float | None
    buckets: dict[str, int] = field(default_factory=dict)
    out_of_sample: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- frame construction -------------------------------------------------

_NUMERIC = {
    "snap_mcap_usd": pl.Float64,
    "snap_liquidity_usd": pl.Float64,
    "snap_price_usd": pl.Float64,
    "quality_score": pl.Float64,
    "peak_multiple": pl.Float64,
    "current_multiple": pl.Float64,
    "mult_15m": pl.Float64,
    "mult_1h": pl.Float64,
    "mult_4h": pl.Float64,
    "mult_24h": pl.Float64,
    "mult_7d": pl.Float64,
}


def to_frame(signals: list[dict[str, Any]]) -> pl.DataFrame:
    """Normalise raw signal rows into a typed frame."""
    if not signals:
        return pl.DataFrame(
            schema={
                "id": pl.Int64,
                "chain_id": pl.Int64,
                "token": pl.Utf8,
                "symbol": pl.Utf8,
                "ts": pl.Datetime(time_zone="UTC"),
                "distinct_clusters": pl.Int64,
                "snap_age_minutes": pl.Int64,
                "safety_verdict": pl.Utf8,
                "is_dead": pl.Boolean,
                **_NUMERIC,
            }
        )

    df = pl.DataFrame(
        [
            {
                "id": row.get("id"),
                "chain_id": row.get("chain_id"),
                "token": row.get("token"),
                "symbol": row.get("symbol"),
                "ts": row.get("ts"),
                "distinct_clusters": row.get("distinct_clusters") or 0,
                "snap_age_minutes": row.get("snap_age_minutes"),
                "safety_verdict": row.get("safety_verdict") or "unknown",
                "is_dead": bool(row.get("is_dead")),
                **{k: _to_float(row.get(k)) for k in _NUMERIC},
            }
            for row in signals
        ],
        strict=False,
    )
    return df.with_columns(
        [pl.col(name).cast(dtype, strict=False) for name, dtype in _NUMERIC.items()]
    )


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce a Polars aggregate (typed as a union) to a plain float."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


# --- filtering ----------------------------------------------------------


def apply_filters(df: pl.DataFrame, spec: FilterSpec) -> pl.DataFrame:
    out = df
    if spec.chains:
        out = out.filter(pl.col("chain_id").is_in(list(spec.chains)))
    if spec.min_clusters is not None:
        out = out.filter(pl.col("distinct_clusters") >= spec.min_clusters)
    if spec.max_clusters is not None:
        out = out.filter(pl.col("distinct_clusters") <= spec.max_clusters)
    # A missing snapshot value must not silently pass a filter: if the user
    # asked for "mcap under 100k" a token with unknown mcap is not an answer.
    if spec.min_mcap_usd is not None:
        out = out.filter(pl.col("snap_mcap_usd") >= spec.min_mcap_usd)
    if spec.max_mcap_usd is not None:
        out = out.filter(pl.col("snap_mcap_usd") <= spec.max_mcap_usd)
    if spec.min_liquidity_usd is not None:
        out = out.filter(pl.col("snap_liquidity_usd") >= spec.min_liquidity_usd)
    if spec.max_liquidity_usd is not None:
        out = out.filter(pl.col("snap_liquidity_usd") <= spec.max_liquidity_usd)
    if spec.max_age_minutes is not None:
        out = out.filter(pl.col("snap_age_minutes") <= spec.max_age_minutes)
    if spec.safety_verdicts:
        out = out.filter(pl.col("safety_verdict").is_in(list(spec.safety_verdicts)))
    if spec.min_quality is not None:
        out = out.filter(pl.col("quality_score") >= spec.min_quality)
    return out


# --- exits and costs ----------------------------------------------------


def _final_expr() -> pl.Expr:
    """Where the position stands now; a dead token is worth zero."""
    return (
        pl.when(pl.col("is_dead"))
        .then(pl.lit(0.0))
        .otherwise(pl.col("current_multiple").fill_null(0.0).clip(lower_bound=0.0))
    )


def _peak_expr() -> pl.Expr:
    return pl.max_horizontal(pl.col("peak_multiple").fill_null(0.0), _final_expr())


def exit_expr(model: ExitModel) -> pl.Expr:
    """Vectorised gross multiple for one exit model."""
    final, peak = _final_expr(), _peak_expr()

    if isinstance(model, HoldToNow):
        return final
    if isinstance(model, FixedTakeProfit):
        return pl.when(peak >= model.target).then(pl.lit(model.target)).otherwise(final)
    if isinstance(model, Ladder):
        realised = pl.lit(0.0)
        remaining = pl.lit(1.0)
        for target, fraction in model.rungs:
            realised = realised + pl.when(peak >= target).then(fraction * target).otherwise(0.0)
            remaining = remaining - pl.when(peak >= target).then(fraction).otherwise(0.0)
        return realised + remaining * final
    if isinstance(model, TimeStop):
        column = f"mult_{model.horizon}"
        return pl.col(column).fill_null(final).clip(lower_bound=0.0)
    if isinstance(model, TrailingStop):
        return pl.max_horizontal(peak * (1.0 - model.drop_pct), final)
    if isinstance(model, PeakFraction):
        return peak * model.fraction
    raise TypeError(f"no vectorised form for {type(model).__name__}")


def apply_costs(df: pl.DataFrame, *, size_usd: float, cost: CostModel) -> pl.DataFrame:
    """Add ``net_multiple`` and ``pnl_usd`` from ``gross_multiple``."""
    fee = cost.dex_fee_bps / 10_000
    entry_extra = (cost.buy_tax_bps + cost.extra_entry_slippage_bps) / 10_000
    exit_extra = cost.sell_tax_bps / 10_000
    liq = pl.col("snap_liquidity_usd")
    gross = pl.col("gross_multiple").clip(lower_bound=0.0)

    entry_slip = size_usd / (liq / 2.0 + size_usd)
    entry_cost = (pl.lit(fee + entry_extra) + entry_slip).clip(upper_bound=0.99)
    tokens_value = size_usd * (1.0 - entry_cost)

    gross_exit = tokens_value * gross
    exit_liq = liq * gross.clip(lower_bound=1e-9).sqrt()
    exit_slip = gross_exit / (exit_liq / 2.0 + gross_exit)
    exit_cost = (pl.lit(fee + exit_extra) + exit_slip).clip(upper_bound=0.99)

    proceeds = gross_exit * (1.0 - exit_cost) - 2.0 * cost.gas_usd_per_swap
    net = (proceeds / size_usd).clip(lower_bound=0.0)

    # A pool too thin to trade against, at entry or at exit, is a zero.
    unexitable = (
        liq.is_null() | (liq < MIN_USABLE_LIQUIDITY_USD) | (exit_liq < MIN_USABLE_LIQUIDITY_USD)
    )
    net = pl.when(unexitable | (gross <= 0)).then(pl.lit(0.0)).otherwise(net)

    round_trip_gross = size_usd * (1.0 - entry_cost)
    flat_exit_cost = (
        pl.lit(fee + exit_extra) + round_trip_gross / (liq / 2.0 + round_trip_gross)
    ).clip(upper_bound=0.99)
    flat_net = (round_trip_gross * (1.0 - flat_exit_cost) - 2.0 * cost.gas_usd_per_swap) / size_usd

    return df.with_columns(
        net_multiple=net,
        pnl_usd=(net - 1.0) * size_usd,
        round_trip_cost_pct=(1.0 - flat_net) * 100.0,
    )


# --- statistics ---------------------------------------------------------


def _bootstrap_ci(
    values: list[float], statistic: str, *, iterations: int = 400, seed: int = 12345
) -> tuple[float, float] | None:
    """Percentile bootstrap interval. Returns None when the sample is too small."""
    n = len(values)
    if n < 12:
        return None
    rng = _Rng(seed)
    stats: list[float] = []
    for _ in range(iterations):
        sample = [values[rng.below(n)] for _ in range(n)]
        if statistic == "median":
            sample.sort()
            stats.append(sample[n // 2] if n % 2 else (sample[n // 2 - 1] + sample[n // 2]) / 2.0)
        else:  # win rate
            stats.append(sum(1 for v in sample if v > 1.0) / n)
    stats.sort()
    return round(stats[int(0.025 * iterations)], 4), round(stats[int(0.975 * iterations) - 1], 4)


class _Rng:
    """Deterministic LCG so identical inputs always give identical intervals."""

    def __init__(self, seed: int) -> None:
        self._state = seed & 0xFFFFFFFF

    def below(self, n: int) -> int:
        self._state = (1664525 * self._state + 1013904223) & 0xFFFFFFFF
        return self._state % n


def bucket_counts(multiples: list[float]) -> dict[str, int]:
    counts = dict.fromkeys(BUCKETS, 0)
    for m in multiples:
        if m < 1.0:
            counts["flat (<1x)"] += 1
        elif m < 2.0:
            counts["1-2x"] += 1
        elif m < 5.0:
            counts["2-5x"] += 1
        elif m < 10.0:
            counts["5-10x"] += 1
        else:
            counts["10x+"] += 1
    return counts


def summarise(
    df: pl.DataFrame,
    *,
    filter_label: str,
    exit_model: ExitModel,
    size_usd: float,
    out_of_sample: dict[str, Any] | None = None,
) -> BacktestResult:
    n = df.height
    warnings: list[str] = []
    if exit_model.uses_look_ahead:
        warnings.append(
            "This exit model needs hindsight (the future peak). Treat it as a "
            "ceiling, not as an achievable result."
        )
    if n == 0:
        return BacktestResult(
            filter_label=filter_label,
            exit_model=exit_model.name,
            uses_look_ahead=exit_model.uses_look_ahead,
            signals=0,
            win_rate=None,
            win_rate_ci=None,
            median_net_multiple=None,
            median_ci=None,
            mean_net_multiple=None,
            total_pnl_usd=0.0,
            roi_pct=None,
            invested_usd=0.0,
            dead_share=None,
            median_round_trip_cost_pct=None,
            warnings=[*warnings, "No signals matched this filter."],
        )
    if n < 30:
        warnings.append(f"Only {n} signals matched - the interval is wide and easy to misread.")

    nets = [v for v in df["net_multiple"].to_list() if v is not None]
    pnl = _num(df["pnl_usd"].sum())
    invested = size_usd * n
    wins = sum(1 for v in nets if v > 1.0)

    return BacktestResult(
        filter_label=filter_label,
        exit_model=exit_model.name,
        uses_look_ahead=exit_model.uses_look_ahead,
        signals=n,
        win_rate=round(wins / len(nets), 4) if nets else None,
        win_rate_ci=_bootstrap_ci(nets, "win_rate"),
        median_net_multiple=round(_num(df["net_multiple"].median()), 4),
        median_ci=_bootstrap_ci(nets, "median"),
        mean_net_multiple=round(_num(df["net_multiple"].mean()), 4),
        total_pnl_usd=round(pnl, 2),
        roi_pct=round(100.0 * pnl / invested, 2) if invested else None,
        invested_usd=round(invested, 2),
        dead_share=round(_num(df["is_dead"].mean()), 4),
        median_round_trip_cost_pct=round(_num(df["round_trip_cost_pct"].median()), 2),
        buckets=bucket_counts(nets),
        out_of_sample=out_of_sample,
        warnings=warnings,
    )


# --- top-level runs -----------------------------------------------------


def run(
    signals: list[dict[str, Any]] | pl.DataFrame,
    *,
    spec: FilterSpec,
    exit_model: ExitModel,
    size_usd: float = 100.0,
    cost: CostModel | None = None,
    split_at: datetime | None = None,
) -> BacktestResult:
    """Run one filter/exit combination, optionally with an out-of-sample split."""
    df = signals if isinstance(signals, pl.DataFrame) else to_frame(signals)
    cost = cost or CostModel()
    filtered = apply_filters(df, spec)
    scored = apply_costs(
        filtered.with_columns(gross_multiple=exit_expr(exit_model)),
        size_usd=size_usd,
        cost=cost,
    )

    oos: dict[str, Any] | None = None
    if split_at is not None and scored.height:
        train = scored.filter(pl.col("ts") < split_at)
        test = scored.filter(pl.col("ts") >= split_at)
        oos = {
            "split_at": split_at.isoformat(),
            "in_sample": _slice_stats(train),
            "out_of_sample": _slice_stats(test),
        }

    return summarise(
        scored,
        filter_label=spec.label(),
        exit_model=exit_model,
        size_usd=size_usd,
        out_of_sample=oos,
    )


def _slice_stats(df: pl.DataFrame) -> dict[str, Any]:
    if not df.height:
        return {"signals": 0, "win_rate": None, "median_net_multiple": None, "roi_pct": None}
    nets = [v for v in df["net_multiple"].to_list() if v is not None]
    invested = df.height * 100.0
    return {
        "signals": df.height,
        "win_rate": round(sum(1 for v in nets if v > 1.0) / len(nets), 4),
        "median_net_multiple": round(_num(df["net_multiple"].median()), 4),
        "roi_pct": round(100.0 * _num(df["pnl_usd"].sum()) / invested, 2),
    }


def search(
    signals: list[dict[str, Any]] | pl.DataFrame,
    *,
    specs: list[FilterSpec],
    exit_models: tuple[ExitModel, ...] = DEFAULT_MODELS,
    size_usd: float = 100.0,
    cost: CostModel | None = None,
    min_signals: int = 25,
    split_at: datetime | None = None,
    include_look_ahead: bool = False,
) -> list[BacktestResult]:
    """Score many combinations and rank them by realistic ROI.

    Look-ahead models are excluded by default: ranking strategies by a number
    that needs hindsight is precisely how a leaderboard becomes fiction.
    """
    df = signals if isinstance(signals, pl.DataFrame) else to_frame(signals)
    models = (
        exit_models
        if include_look_ahead
        else tuple(m for m in exit_models if not m.uses_look_ahead)
    )
    results = [
        run(df, spec=spec, exit_model=model, size_usd=size_usd, cost=cost, split_at=split_at)
        for spec in specs
        for model in models
    ]
    eligible = [r for r in results if r.signals >= min_signals]
    return sorted(eligible, key=lambda r: r.roi_pct or -1e9, reverse=True)
