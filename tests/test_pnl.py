from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coinfinder.scoring.pnl import compute_round_trips, summarise

T0 = datetime(2026, 9, 1, tzinfo=UTC)
W = "0xwallet"
TOK = "0xtoken"


def tr(minute, side, qty, usd, token=TOK, wallet=W, decimals=18):
    return {
        "wallet": wallet,
        "token": token,
        "ts": T0 + timedelta(minutes=minute),
        "side": side,
        "token_amount": int(qty * 10**decimals),
        "usd_value": usd,
        "decimals": decimals,
    }


def test_simple_double():
    r = compute_round_trips([tr(0, "buy", 100, 100.0), tr(60, "sell", 100, 200.0)])
    assert len(r.round_trips) == 1
    rt = r.round_trips[0]
    assert rt.multiple == pytest.approx(2.0)
    assert rt.pnl_usd == pytest.approx(100.0)
    assert rt.hold_minutes == pytest.approx(60.0)
    assert not r.open_positions


def test_partial_exits_accumulate_into_one_round_trip():
    r = compute_round_trips(
        [tr(0, "buy", 100, 100.0), tr(10, "sell", 50, 90.0), tr(20, "sell", 50, 150.0)]
    )
    assert len(r.round_trips) == 1
    assert r.round_trips[0].proceeds_usd == pytest.approx(240.0)
    assert r.round_trips[0].multiple == pytest.approx(2.4)


def test_scaling_in_then_out():
    r = compute_round_trips(
        [tr(0, "buy", 100, 100.0), tr(5, "buy", 100, 300.0), tr(10, "sell", 200, 800.0)]
    )
    assert r.round_trips[0].cost_usd == pytest.approx(400.0)
    assert r.round_trips[0].multiple == pytest.approx(2.0)


def test_open_position_is_not_a_round_trip():
    r = compute_round_trips([tr(0, "buy", 100, 100.0)])
    assert r.round_trips == []
    assert len(r.open_positions) == 1
    assert r.open_positions[0].cost_usd == pytest.approx(100.0)


def test_partial_sale_leaves_position_open():
    r = compute_round_trips([tr(0, "buy", 100, 100.0), tr(5, "sell", 30, 60.0)])
    assert r.round_trips == []
    assert r.open_positions[0].qty == pytest.approx(70.0)


def test_dust_remainder_closes_the_position():
    # Selling 99.5% leaves dust; the position must still count as closed.
    r = compute_round_trips([tr(0, "buy", 100, 100.0), tr(5, "sell", 99.5, 500.0)])
    assert len(r.round_trips) == 1
    assert r.round_trips[0].multiple == pytest.approx(5.0)


def test_airdrop_sell_without_buy_is_excluded():
    # An unmatched sell has no cost basis; counting it would fake an infinite
    # multiple, which is the single easiest way to corrupt a wallet score.
    r = compute_round_trips([tr(0, "sell", 1000, 5000.0)])
    assert r.round_trips == []
    assert r.unmatched_sells == 1


def test_trades_without_usd_value_are_skipped():
    t = tr(0, "buy", 100, None)
    r = compute_round_trips([t, tr(60, "sell", 100, 200.0)])
    assert r.round_trips == []
    assert r.unmatched_sells == 1


def test_reentry_after_close_creates_two_round_trips():
    r = compute_round_trips(
        [
            tr(0, "buy", 100, 100.0),
            tr(10, "sell", 100, 300.0),
            tr(20, "buy", 50, 100.0),
            tr(30, "sell", 50, 50.0),
        ]
    )
    assert [round(rt.multiple, 2) for rt in r.round_trips] == [3.0, 0.5]


def test_multiple_tokens_are_tracked_independently():
    r = compute_round_trips(
        [
            tr(0, "buy", 100, 100.0, token="0xa"),
            tr(1, "buy", 100, 100.0, token="0xb"),
            tr(2, "sell", 100, 50.0, token="0xa"),
            tr(3, "sell", 100, 400.0, token="0xb"),
        ]
    )
    by_token = {rt.token: rt.multiple for rt in r.round_trips}
    assert by_token == pytest.approx({"0xa": 0.5, "0xb": 4.0})


def test_non_18_decimals_handled():
    r = compute_round_trips(
        [tr(0, "buy", 1000, 1000.0, decimals=6), tr(5, "sell", 1000, 2000.0, decimals=6)]
    )
    assert r.round_trips[0].multiple == pytest.approx(2.0)


def test_summarise_matches_hand_computed_values():
    r = compute_round_trips(
        [
            tr(0, "buy", 100, 100.0, token="0xa"),
            tr(10, "sell", 100, 300.0, token="0xa"),  # 3x, +200
            tr(0, "buy", 100, 100.0, token="0xb"),
            tr(20, "sell", 100, 50.0, token="0xb"),  # 0.5x, -50
            tr(0, "buy", 100, 100.0, token="0xc"),
            tr(30, "sell", 100, 100.0, token="0xc"),  # 1.0x, flat
        ]
    )
    s = summarise(r.round_trips)
    assert s["closed_trades"] == 3
    assert s["wins"] == 1  # only >1x counts
    assert s["win_rate"] == pytest.approx(1 / 3)
    assert s["median_multiple"] == pytest.approx(1.0)
    assert s["realized_pnl_usd"] == pytest.approx(150.0)
    assert s["profit_factor"] == pytest.approx(200 / 50)
    assert s["distinct_tokens"] == 3


def test_summarise_empty():
    s = summarise([])
    assert s["closed_trades"] == 0 and s["win_rate"] is None
