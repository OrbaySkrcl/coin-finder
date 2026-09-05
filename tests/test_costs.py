from __future__ import annotations

import pytest

from coinfinder.backtest.costs import (
    CostModel,
    liquidity_at_exit,
    net_multiple,
    net_pnl_usd,
    round_trip_cost_pct,
)

M = CostModel(dex_fee_bps=30, gas_usd_per_swap=0.02)


def test_slippage_follows_constant_product_maths():
    # Quote reserve is half of reported liquidity: 100 / (20000 + 100).
    assert M.slippage_fraction(100.0, 40_000.0) == pytest.approx(100 / 20_100)


def test_slippage_grows_with_size_and_shrinks_with_depth():
    assert M.slippage_fraction(1_000.0, 40_000.0) > M.slippage_fraction(100.0, 40_000.0)
    assert M.slippage_fraction(100.0, 400_000.0) < M.slippage_fraction(100.0, 40_000.0)


def test_unusable_liquidity_means_no_exit():
    assert M.slippage_fraction(100.0, 10.0) == 1.0
    assert net_multiple(5.0, size_usd=100, entry_liquidity_usd=10.0, model=M) == 0.0
    assert net_multiple(5.0, size_usd=100, entry_liquidity_usd=None, model=M) == 0.0


def test_costs_always_reduce_the_multiple():
    for gross in (0.5, 1.0, 1.54, 10.0):
        net = net_multiple(gross, size_usd=100, entry_liquidity_usd=40_778.0, model=M)
        assert net < gross


def test_flat_trade_loses_the_round_trip_cost():
    net = net_multiple(1.0, size_usd=100, entry_liquidity_usd=40_778.0, model=M)
    assert 0.97 < net < 1.0
    assert round_trip_cost_pct(
        size_usd=100, entry_liquidity_usd=40_778.0, model=M
    ) == pytest.approx(100 * (1 - net))


def test_position_size_can_turn_a_winner_into_a_loser():
    # The headline claim of the reference product is a 1.54x median. At $5k
    # into a $40.8k pool that is a losing trade after execution costs.
    small = net_multiple(1.54, size_usd=100, entry_liquidity_usd=40_778.0, model=M)
    large = net_multiple(1.54, size_usd=5_000, entry_liquidity_usd=40_778.0, model=M)
    assert small > 1.5
    assert large < 1.0


def test_token_tax_is_charged_on_both_legs():
    taxed = CostModel(dex_fee_bps=30, gas_usd_per_swap=0.02, buy_tax_bps=1000, sell_tax_bps=1000)
    a = net_multiple(1.54, size_usd=100, entry_liquidity_usd=40_778.0, model=M)
    b = net_multiple(1.54, size_usd=100, entry_liquidity_usd=40_778.0, model=taxed)
    assert b < a * 0.85


def test_gas_matters_more_on_small_positions():
    gassy = CostModel(dex_fee_bps=30, gas_usd_per_swap=2.0)
    tiny = net_multiple(1.5, size_usd=20, entry_liquidity_usd=100_000.0, model=gassy)
    big = net_multiple(1.5, size_usd=2_000, entry_liquidity_usd=100_000.0, model=gassy)
    assert tiny < big


def test_liquidity_at_exit_scales_with_sqrt_of_multiple():
    assert liquidity_at_exit(40_000.0, 4.0) == pytest.approx(80_000.0)
    assert liquidity_at_exit(40_000.0, 1.0) == pytest.approx(40_000.0)
    assert liquidity_at_exit(None, 4.0) is None


def test_net_multiple_never_goes_negative():
    assert net_multiple(0.0, size_usd=100, entry_liquidity_usd=40_000.0, model=M) == 0.0
    assert net_multiple(-1.0, size_usd=100, entry_liquidity_usd=40_000.0, model=M) == 0.0


def test_net_pnl_matches_net_multiple():
    pnl = net_pnl_usd(2.0, size_usd=100, entry_liquidity_usd=40_000.0, model=M)
    mult = net_multiple(2.0, size_usd=100, entry_liquidity_usd=40_000.0, model=M)
    assert pnl == pytest.approx(100 * (mult - 1.0))
