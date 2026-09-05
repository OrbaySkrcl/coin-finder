"""FIFO realised-PnL accounting.

A wallet is judged on *completed* round trips only. Marking open positions to
market would let a wallet look brilliant for holding an illiquid token it can
never actually sell, which is exactly the illusion this project exists to
avoid.

Edge cases that matter in practice:

* **Airdrops and inbound transfers.** A sell with no matching buy has no cost
  basis. Counting it would produce an infinite multiple, so unmatched sells are
  recorded separately and excluded from scoring.
* **Dust.** Tokens with fee-on-transfer maths rarely leave a balance at exactly
  zero, so a position counts as closed once under ``DUST_FRACTION`` of its peak.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Any

DUST_FRACTION = 0.01  # under 1% of peak size counts as closed


@dataclass(slots=True)
class Lot:
    qty: float
    cost_usd: float
    opened_at: datetime


@dataclass(slots=True)
class RoundTrip:
    wallet: str
    token: str
    cost_usd: float
    proceeds_usd: float
    opened_at: datetime
    closed_at: datetime

    @property
    def multiple(self) -> float:
        return self.proceeds_usd / self.cost_usd if self.cost_usd > 0 else 0.0

    @property
    def pnl_usd(self) -> float:
        return self.proceeds_usd - self.cost_usd

    @property
    def hold_minutes(self) -> float:
        return max(0.0, (self.closed_at - self.opened_at).total_seconds() / 60.0)


@dataclass(slots=True)
class OpenPosition:
    wallet: str
    token: str
    qty: float
    cost_usd: float
    opened_at: datetime


@dataclass(slots=True)
class PnLResult:
    round_trips: list[RoundTrip] = field(default_factory=list)
    open_positions: list[OpenPosition] = field(default_factory=list)
    unmatched_sells: int = 0


def _qty(trade: dict[str, Any]) -> float:
    """Token amount in whole units. Raw integers keep full precision upstream."""
    raw = trade.get("token_amount") or 0
    decimals = int(trade.get("decimals") or 18)
    return float(raw) / (10**decimals)


def compute_round_trips(trades: list[dict[str, Any]]) -> PnLResult:
    """Walk one wallet's trades in time order and close positions FIFO.

    ``trades`` must each carry: wallet, token, ts, side, token_amount,
    usd_value. Trades without a usd_value are skipped - guessing a price would
    corrupt the score.
    """
    result = PnLResult()
    lots: dict[tuple[str, str], deque[Lot]] = defaultdict(deque)
    peak_qty: dict[tuple[str, str], float] = defaultdict(float)
    episode: dict[tuple[str, str], dict[str, Any]] = {}

    for trade in sorted(trades, key=lambda t: (t["wallet"], t["token"], t["ts"])):
        usd = trade.get("usd_value")
        if usd is None:
            continue
        usd = float(usd)
        qty = _qty(trade)
        if qty <= 0:
            continue

        key = (trade["wallet"], trade["token"])
        ts: datetime = trade["ts"]

        if trade["side"] == "buy":
            lots[key].append(Lot(qty=qty, cost_usd=usd, opened_at=ts))
            open_qty = sum(lot.qty for lot in lots[key])
            peak_qty[key] = max(peak_qty[key], open_qty)
            ep = episode.setdefault(key, {"cost": 0.0, "proceeds": 0.0, "opened_at": ts})
            ep["cost"] += usd
            continue

        # --- sell ---
        if not lots[key]:
            result.unmatched_sells += 1
            continue

        remaining = qty
        while remaining > 1e-18 and lots[key]:
            lot = lots[key][0]
            take = min(lot.qty, remaining)
            lot.qty -= take
            remaining -= take
            if lot.qty <= 1e-18:
                lots[key].popleft()

        ep = episode.setdefault(key, {"cost": 0.0, "proceeds": 0.0, "opened_at": ts})
        ep["proceeds"] += usd

        open_qty = sum(lot.qty for lot in lots[key])
        if open_qty <= peak_qty[key] * DUST_FRACTION:
            if ep["cost"] > 0:
                result.round_trips.append(
                    RoundTrip(
                        wallet=key[0],
                        token=key[1],
                        cost_usd=ep["cost"],
                        proceeds_usd=ep["proceeds"],
                        opened_at=ep["opened_at"],
                        closed_at=ts,
                    )
                )
            lots[key].clear()
            peak_qty[key] = 0.0
            episode.pop(key, None)

    for key, remaining_lots in lots.items():
        open_qty = sum(lot.qty for lot in remaining_lots)
        if open_qty <= 0:
            continue
        result.open_positions.append(
            OpenPosition(
                wallet=key[0],
                token=key[1],
                qty=open_qty,
                cost_usd=sum(lot.cost_usd for lot in remaining_lots),
                opened_at=min(lot.opened_at for lot in remaining_lots),
            )
        )
    return result


def summarise(round_trips: list[RoundTrip]) -> dict[str, Any]:
    """Headline statistics for a set of round trips."""
    if not round_trips:
        return {
            "closed_trades": 0,
            "wins": 0,
            "win_rate": None,
            "median_multiple": None,
            "realized_pnl_usd": 0.0,
            "avg_hold_minutes": None,
            "distinct_tokens": 0,
            "profit_factor": None,
            "total_invested_usd": 0.0,
        }
    multiples = [rt.multiple for rt in round_trips]
    wins = sum(1 for m in multiples if m > 1.0)
    gains = sum(rt.pnl_usd for rt in round_trips if rt.pnl_usd > 0)
    losses = -sum(rt.pnl_usd for rt in round_trips if rt.pnl_usd < 0)
    return {
        "closed_trades": len(round_trips),
        "wins": wins,
        "win_rate": wins / len(round_trips),
        "median_multiple": median(multiples),
        "realized_pnl_usd": sum(rt.pnl_usd for rt in round_trips),
        "avg_hold_minutes": sum(rt.hold_minutes for rt in round_trips) / len(round_trips),
        "distinct_tokens": len({rt.token for rt in round_trips}),
        "profit_factor": (gains / losses) if losses > 0 else None,
        "total_invested_usd": sum(rt.cost_usd for rt in round_trips),
    }
