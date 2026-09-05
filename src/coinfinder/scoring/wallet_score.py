"""Turn round trips into a smart-wallet ranking.

Three problems have to be solved at once:

1. **Small samples lie.** A wallet with 3 wins out of 3 is not a 100% win-rate
   trader. Win rate is shrunk toward a prior, so a short record cannot top the
   ranking on luck alone.
2. **Old edge decays.** Performance is weighted by an exponential half-life, so
   a wallet that was brilliant two months ago and quiet since drifts down.
3. **Bots are not smart money.** Snipers and MEV bots have superb statistics and
   are useless as a signal - nobody can copy a trade that closes in 4 seconds.
   They are excluded on behaviour, not on PnL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

from coinfinder.scoring.pnl import RoundTrip, summarise

# Beta prior for win rate. Mean 2/(2+3) = 40%, which is roughly what an
# unfiltered memecoin trader achieves, so wallets must beat that to score.
PRIOR_WINS = 2.0
PRIOR_LOSSES = 3.0

# Behavioural exclusions.
MIN_AVG_HOLD_MINUTES = 3.0
MAX_TRADES_PER_DAY = 40.0
MIN_DISTINCT_TOKENS = 4
#: Frequency is only judged once a wallet has been observable this long.
MIN_SPAN_DAYS_FOR_FREQUENCY = 2.0

#: PnL contribution saturates here so one lucky 500x cannot dominate.
PNL_SATURATION_USD = 250_000.0


@dataclass(slots=True)
class WalletScore:
    wallet: str
    score: float
    closed_trades: int
    wins: int
    win_rate: float | None
    shrunk_win_rate: float
    median_multiple: float | None
    realized_pnl_usd: float
    weighted_pnl_usd: float
    avg_hold_minutes: float | None
    distinct_tokens: int
    profit_factor: float | None
    is_smart: bool
    excluded_reason: str | None = None

    def as_row(self, window_days: int) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "window_days": window_days,
            "closed_trades": self.closed_trades,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "median_multiple": self.median_multiple,
            "realized_pnl_usd": self.realized_pnl_usd,
            "avg_hold_minutes": self.avg_hold_minutes,
            "distinct_tokens": self.distinct_tokens,
            "score": self.score,
            "is_smart": self.is_smart,
        }


def decay_weight(closed_at: datetime, now: datetime, halflife_days: float) -> float:
    age_days = max(0.0, (now - closed_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / halflife_days)


def shrunk_win_rate(wins: int, total: int) -> float:
    return (wins + PRIOR_WINS) / (total + PRIOR_WINS + PRIOR_LOSSES)


def score_wallet(
    wallet: str,
    round_trips: list[RoundTrip],
    *,
    now: datetime,
    halflife_days: float = 30.0,
    min_trades: int = 8,
) -> WalletScore:
    stats = summarise(round_trips)
    n = stats["closed_trades"]

    weighted_pnl = sum(
        rt.pnl_usd * decay_weight(rt.closed_at, now, halflife_days) for rt in round_trips
    )
    swr = shrunk_win_rate(stats["wins"], n)

    base = WalletScore(
        wallet=wallet,
        score=0.0,
        closed_trades=n,
        wins=stats["wins"],
        win_rate=stats["win_rate"],
        shrunk_win_rate=swr,
        median_multiple=stats["median_multiple"],
        realized_pnl_usd=stats["realized_pnl_usd"],
        weighted_pnl_usd=weighted_pnl,
        avg_hold_minutes=stats["avg_hold_minutes"],
        distinct_tokens=stats["distinct_tokens"],
        profit_factor=stats["profit_factor"],
        is_smart=False,
    )

    # --- hard exclusions ------------------------------------------------
    if n < min_trades:
        base.excluded_reason = "too_few_trades"
        return base
    if stats["distinct_tokens"] < MIN_DISTINCT_TOKENS:
        base.excluded_reason = "too_few_tokens"
        return base
    if (stats["avg_hold_minutes"] or 0) < MIN_AVG_HOLD_MINUTES:
        # Sub-3-minute average hold is a sniper or MEV bot: unfollowable.
        base.excluded_reason = "bot_like_hold"
        return base
    if weighted_pnl <= 0:
        base.excluded_reason = "not_profitable"
        return base

    # Fractional days, not .days - truncating to whole days made a wallet with
    # a single active afternoon look like a 60-trades-per-day bot. The filter
    # also needs a real observation window before it can judge frequency, so a
    # wallet we have only just started watching is never excluded on a burst.
    span_days = (
        max(rt.closed_at for rt in round_trips) - min(rt.opened_at for rt in round_trips)
    ).total_seconds() / 86400.0
    if span_days >= MIN_SPAN_DAYS_FOR_FREQUENCY and n / span_days > MAX_TRADES_PER_DAY:
        base.excluded_reason = "bot_like_frequency"
        return base

    # --- composite score ------------------------------------------------
    # Each term is squashed to roughly 0..1 so no single dimension dominates.
    pnl_term = math.log1p(max(0.0, weighted_pnl)) / math.log1p(PNL_SATURATION_USD)
    win_term = min(1.0, swr / 0.65)  # 65% shrunk win rate saturates the term
    med = stats["median_multiple"] or 0.0
    mult_term = min(1.0, max(0.0, med - 1.0) / 1.5)  # 2.5x median saturates
    # A wallet that is right on many different tokens is more credible than
    # one that got a single name right repeatedly.
    breadth_term = min(1.0, stats["distinct_tokens"] / 25.0)

    base.score = round(
        100.0 * (0.40 * pnl_term + 0.25 * win_term + 0.20 * mult_term + 0.15 * breadth_term), 3
    )
    base.is_smart = True
    return base


def rank_wallets(
    per_wallet: dict[str, list[RoundTrip]],
    *,
    now: datetime,
    halflife_days: float = 30.0,
    min_trades: int = 8,
    top_n: int = 600,
) -> list[WalletScore]:
    """Score every wallet and return the eligible ones, best first."""
    scored = [
        score_wallet(wallet, trips, now=now, halflife_days=halflife_days, min_trades=min_trades)
        for wallet, trips in per_wallet.items()
    ]
    smart = sorted((s for s in scored if s.is_smart), key=lambda s: s.score, reverse=True)[:top_n]
    keep = {s.wallet for s in smart}
    return smart + [s for s in scored if s.wallet not in keep]


def group_round_trips(round_trips: list[RoundTrip]) -> dict[str, list[RoundTrip]]:
    out: dict[str, list[RoundTrip]] = {}
    for rt in round_trips:
        out.setdefault(rt.wallet, []).append(rt)
    return out


def median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None
