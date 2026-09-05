"""Execution cost model.

The reference product reports a 1.54x median multiple. That number is gross:
it ignores the swap fee, the price impact of trading into a $40k pool, token
taxes and gas. On lowcap tokens those together are worth several percent of a
round trip, which is the difference between a strategy that works and one that
does not.

Slippage is derived from constant-product AMM maths rather than guessed. For a
pool holding ``X`` of the quote asset, buying with ``dx`` moves the effective
price by ``dx / (X + dx)``. DexScreener reports total pool liquidity in USD
counting both sides, so the quote reserve is half of it.

Liquidity at exit is scaled by ``sqrt(multiple)``: in a constant-product pool
with unchanged LP positions, pool USD value grows with the square root of the
price change. That keeps exit slippage from being wildly overstated on a
winner, without pretending a 50x exit is frictionless.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Below this the AMM approximation is meaningless; treat the exit as impossible.
MIN_USABLE_LIQUIDITY_USD = 250.0


@dataclass(frozen=True, slots=True)
class CostModel:
    dex_fee_bps: int = 30
    gas_usd_per_swap: float = 0.05
    buy_tax_bps: int = 0
    sell_tax_bps: int = 0
    #: Extra padding for MEV/priority competition on entry.
    extra_entry_slippage_bps: int = 0

    def slippage_fraction(self, trade_usd: float, liquidity_usd: float | None) -> float:
        """Price impact of one trade against a pool, as a fraction of size."""
        if not liquidity_usd or liquidity_usd < MIN_USABLE_LIQUIDITY_USD:
            return 1.0  # unexitable
        quote_reserve = liquidity_usd / 2.0
        return trade_usd / (quote_reserve + trade_usd)

    def is_exitable(self, liquidity_usd: float | None) -> bool:
        """A pool too thin to price a trade against cannot be exited at all."""
        return bool(liquidity_usd) and liquidity_usd >= MIN_USABLE_LIQUIDITY_USD  # type: ignore[operator]

    def entry_friction(self, size_usd: float, liquidity_usd: float | None) -> float:
        return min(
            0.99,
            self.dex_fee_bps / 10_000
            + self.buy_tax_bps / 10_000
            + self.extra_entry_slippage_bps / 10_000
            + self.slippage_fraction(size_usd, liquidity_usd),
        )

    def exit_friction(self, exit_usd: float, liquidity_at_exit_usd: float | None) -> float:
        return min(
            0.99,
            self.dex_fee_bps / 10_000
            + self.sell_tax_bps / 10_000
            + self.slippage_fraction(exit_usd, liquidity_at_exit_usd),
        )


def liquidity_at_exit(entry_liquidity_usd: float | None, gross_multiple: float) -> float | None:
    """Pool USD value scales with sqrt(price change) in a constant-product AMM."""
    if entry_liquidity_usd is None:
        return None
    return entry_liquidity_usd * math.sqrt(max(1e-9, gross_multiple))


def net_multiple(
    gross_multiple: float,
    *,
    size_usd: float,
    entry_liquidity_usd: float | None,
    model: CostModel,
) -> float:
    """Convert a quoted multiple into what a trader would actually keep."""
    if gross_multiple <= 0:
        return 0.0
    # An unexitable pool is worth zero, not "almost zero". Letting the 99%
    # friction cap leak a fraction of a percent through would understate rugs
    # in every aggregate the backtest reports.
    if not model.is_exitable(entry_liquidity_usd):
        return 0.0

    entry_cost = model.entry_friction(size_usd, entry_liquidity_usd)
    tokens_value = size_usd * (1.0 - entry_cost)

    gross_exit_usd = tokens_value * gross_multiple
    exit_liq = liquidity_at_exit(entry_liquidity_usd, gross_multiple)
    if not model.is_exitable(exit_liq):
        return 0.0
    exit_cost = model.exit_friction(gross_exit_usd, exit_liq)

    proceeds = gross_exit_usd * (1.0 - exit_cost) - 2.0 * model.gas_usd_per_swap
    return max(0.0, proceeds / size_usd)


def net_pnl_usd(
    gross_multiple: float,
    *,
    size_usd: float,
    entry_liquidity_usd: float | None,
    model: CostModel,
) -> float:
    return size_usd * (
        net_multiple(
            gross_multiple,
            size_usd=size_usd,
            entry_liquidity_usd=entry_liquidity_usd,
            model=model,
        )
        - 1.0
    )


def round_trip_cost_pct(
    *, size_usd: float, entry_liquidity_usd: float | None, model: CostModel
) -> float:
    """Cost of entering and immediately exiting, as a percentage of size.

    This is the number to show a user next to any median multiple: a strategy
    whose median gross multiple is below ``1 + this`` loses money on the median
    trade.
    """
    return 100.0 * (
        1.0
        - net_multiple(1.0, size_usd=size_usd, entry_liquidity_usd=entry_liquidity_usd, model=model)
    )
