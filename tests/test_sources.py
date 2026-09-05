"""Parsing tests run entirely from fixtures - no network required."""

from __future__ import annotations

from datetime import UTC, datetime

from coinfinder.sources.dexscreener import best_pair, parse_pair
from coinfinder.sources.geckoterminal import Candle, peak_and_path

FULL_PAIR = {
    "chainId": "base",
    "dexId": "uniswap",
    "pairAddress": "0xPaIr",
    "baseToken": {"address": "0xAAA", "symbol": "COLLECT", "name": "Collect"},
    "quoteToken": {"address": "0xWETH", "symbol": "WETH"},
    "priceUsd": "0.000197",
    "liquidity": {"usd": 40778.0},
    "fdv": 190933,
    "marketCap": 190933,
    "volume": {"h24": 12345},
    "txns": {"h24": {"buys": 30, "sells": 12}},
    "priceChange": {"h24": 23.5},
    "pairCreatedAt": 1735689600000,
}


def test_parse_pair_full():
    p = parse_pair(FULL_PAIR)
    assert p is not None
    assert p.base_address == "0xaaa"  # lower-cased
    assert p.pair_address == "0xpair"
    assert p.price_usd == 0.000197
    assert p.liquidity_usd == 40778.0
    assert p.buys_24h == 30
    assert round(p.liquidity_to_mcap * 100, 1) == 21.4


def test_parse_pair_tolerates_missing_fields():
    p = parse_pair({"baseToken": {"address": "0xB"}})
    assert p is not None and p.price_usd is None and p.mcap_usd is None
    assert p.age_minutes is None


def test_parse_pair_rejects_missing_base_token():
    assert parse_pair({"chainId": "base"}) is None
    assert parse_pair({"baseToken": {}}) is None


def test_parse_pair_rejects_junk_numbers():
    p = parse_pair({**FULL_PAIR, "priceUsd": "not-a-number", "fdv": None, "marketCap": None})
    assert p is not None and p.price_usd is None and p.mcap_usd is None


def test_mcap_falls_back_to_fdv():
    p = parse_pair({**FULL_PAIR, "marketCap": None})
    assert p is not None and p.mcap_usd == 190933.0


def test_best_pair_picks_deepest_liquidity():
    shallow = parse_pair(FULL_PAIR)
    deep = parse_pair({**FULL_PAIR, "liquidity": {"usd": 999999.0}})
    assert best_pair([shallow, deep], "0xAAA").liquidity_usd == 999999.0
    assert best_pair([shallow, deep], "0xother") is None


def test_age_minutes_from_creation():
    p = parse_pair(FULL_PAIR)
    assert p.created_at == datetime(2025, 1, 1, tzinfo=UTC)
    assert p.age_minutes > 0


def _c(ts, o, h, low, c):
    return Candle(datetime.fromtimestamp(ts, tz=UTC), o, h, low, c, 0.0)


def test_peak_and_path():
    candles = [_c(0, 1, 2, 0.9, 1.5), _c(60, 1.5, 5, 1.4, 2.0), _c(120, 2.0, 2.1, 0.5, 0.6)]
    out = peak_and_path(candles, entry_price=1.0)
    assert out["peak_multiple"] == 5.0
    assert out["final_multiple"] == 0.6
    assert out["min_multiple"] == 0.5


def test_peak_and_path_handles_empty():
    assert peak_and_path([], 1.0)["peak_multiple"] is None
    assert peak_and_path([_c(0, 1, 1, 1, 1)], 0.0)["peak_multiple"] is None
