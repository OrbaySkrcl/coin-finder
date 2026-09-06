"""Alert and stats rendering, including the per-size economics."""

from __future__ import annotations

import pytest

from coinfinder.bot.format import (
    break_even_multiple,
    duration,
    format_signal,
    pct,
    signal_links,
    tr_num,
    trade_economics,
    usd,
)
from coinfinder.chains import BASE, BSC, ROBINHOOD

SIGNAL = {
    "token": "0xa1243aa393fe014b65a0d925a54a0165385ae26d",
    "symbol": "COLLECT",
    "distinct_wallets": 5,
    "distinct_clusters": 3,
    "snap_mcap_usd": 190_933,
    "snap_liquidity_usd": 40_778,
    "snap_price_usd": 0.000197,
    "snap_age_minutes": 110,
    "usd_spent": 1200.0,
    "safety_verdict": "caution",
    "safety_flags": ["lp_not_burned", "owner_not_renounced"],
    "quality_p2x": 0.34,
}


# --- Turkish number formatting -----------------------------------------


def test_turkish_decimal_and_thousands_separators():
    assert tr_num(1234.5, 2) == "1.234,50"
    assert tr_num(0.34, 2) == "0,34"
    assert tr_num(1_234_567.891, 1) == "1.234.567,9"


def test_percent_sign_goes_before_the_number():
    assert pct(24.85) == "%24,9"
    assert pct(1.1, 2) == "%1,10"


def test_usd_scales_and_keeps_the_sign():
    assert usd(190_933) == "$190,9B"
    assert usd(1_500_000) == "$1,5M"
    assert usd(10) == "$10"
    assert usd(-27_400) == "-$27,4B"
    assert usd(None) == "?"


def test_usd_drops_a_trailing_zero_decimal():
    # "$500,0B" reads as noise next to "$190,9B"; the zero carries nothing.
    assert usd(500_000) == "$500B"
    assert usd(2_000_000) == "$2M"


def test_price_is_never_abbreviated():
    # usd() compacts thousands into "B", which is right for a market cap and
    # badly wrong for a price: WETH at $2501.79 must not render as "$2,5B".
    from coinfinder.bot.format import usd_price

    assert usd_price(2501.79) == "$2.501,79"
    assert usd(2501.79) == "$2,5B"
    assert usd_price(0.000197) == "$0,000197"
    assert usd_price(None) == "?"


def test_duration_is_turkish():
    assert duration(45) == "45dk"
    assert duration(110) == "1sa 50dk"
    assert duration(1440) == "1g"
    assert duration(None) == "?"


# --- economics ---------------------------------------------------------


def test_break_even_inverts_the_cost():
    assert break_even_multiple(0.0) == 1.0
    assert break_even_multiple(50.0) == pytest.approx(2.0)
    assert break_even_multiple(100.0) == float("inf")


def test_small_positions_are_cheaper_on_base_than_on_bnb():
    """Gas is per transaction, so it dominates a small position.

    BNB Chain gas is several times Base's, which barely matters at $500 and
    matters enormously at $10. This is the asymmetry the sizing control exists
    to surface.
    """
    base_cost, _ = trade_economics(liquidity_usd=40_000, chain=BASE, trade_size_usd=10)
    bnb_cost, _ = trade_economics(liquidity_usd=40_000, chain=BSC, trade_size_usd=10)
    assert bnb_cost > base_cost * 2

    # At a large size the gap all but disappears: slippage dominates instead.
    base_big, _ = trade_economics(liquidity_usd=40_000, chain=BASE, trade_size_usd=500)
    bnb_big, _ = trade_economics(liquidity_usd=40_000, chain=BSC, trade_size_usd=500)
    assert bnb_big / base_big < 1.05


def test_cost_curve_has_a_minimum_rather_than_falling_forever():
    # Gas share falls with size while slippage rises, so there is a cheapest
    # size. Bigger is not automatically better.
    costs = {
        size: trade_economics(liquidity_usd=40_000, chain=BASE, trade_size_usd=size)[0]
        for size in (5, 20, 100, 1000)
    }
    assert costs[20] < costs[5]
    assert costs[20] < costs[1000]


def test_thin_pool_is_flagged_as_unexitable():
    cost, _ = trade_economics(liquidity_usd=50.0, chain=BASE, trade_size_usd=10)
    assert cost >= 99.0


# --- alert rendering ---------------------------------------------------


def test_alert_shows_sybil_collapse():
    text = format_signal(SIGNAL, BASE, trade_size_usd=10)
    assert "5</b> → <b>3 bağımsız" in text


def test_alert_shows_a_single_number_when_nothing_collapsed():
    text = format_signal({**SIGNAL, "distinct_wallets": 3}, BASE, trade_size_usd=10)
    assert "Bağımsız akıllı cüzdan: <b>3</b>" in text


def test_alert_prices_the_round_trip_at_the_readers_size():
    small = format_signal(SIGNAL, BASE, trade_size_usd=10)
    large = format_signal(SIGNAL, BASE, trade_size_usd=500)
    assert "$10 için gidiş-dönüş" in small
    assert "$500 için gidiş-dönüş" in large
    assert small != large


def test_alert_warns_when_gas_dominates_the_cost():
    # $10 on BNB Chain: gas is the majority of the round trip, and the fix is
    # a different chain rather than a different filter.
    text = format_signal(SIGNAL, BSC, trade_size_usd=10)
    assert "⛽" in text and "pahalı" in text

    # The same position on Base is gas-light, so no warning.
    assert "⛽" not in format_signal(SIGNAL, BASE, trade_size_usd=100)


def test_alert_always_says_tax_is_excluded():
    # Token tax cannot be simulated on free RPC and is the largest cost at
    # small sizes, so its absence must never be silent.
    assert "vergisi bu hesaba dahil değil" in format_signal(SIGNAL, BASE, trade_size_usd=10)


def test_alert_marks_an_unexitable_pool():
    text = format_signal({**SIGNAL, "snap_liquidity_usd": 60}, BASE, trade_size_usd=10)
    assert "çıkılamaz" in text


def test_alert_translates_safety_flags():
    text = format_signal(SIGNAL, BASE, trade_size_usd=10)
    assert "LP yakılmamış" in text
    assert "sahiplik bırakılmamış" in text
    assert "🟡 Dikkat" in text


def test_robinhood_alert_carries_the_dyor_flag():
    text = format_signal(
        {**SIGNAL, "safety_flags": ["no_risk_tooling_on_chain"]},
        ROBINHOOD,
        trade_size_usd=10,
    )
    assert "kendin araştır" in text


def test_alert_survives_a_signal_with_almost_no_data():
    text = format_signal({"token": "0xabc"}, BASE, trade_size_usd=10)
    assert "BİLİNMİYOR" in text and "0xabc" in text


def test_probability_is_shown_as_a_percentage():
    assert "2x'e ulaşma olasılığı: <b>%34</b>" in format_signal(SIGNAL, BASE, trade_size_usd=10)


def test_links_are_turkish_and_point_at_the_token():
    labels = [name for name, _ in signal_links(SIGNAL, BASE)]
    assert "📈 Grafik" in labels and "🐦 X'te ara" in labels
    urls = dict(signal_links(SIGNAL, BASE))
    assert SIGNAL["token"] in urls["🔍 Explorer"]


# --- panel link --------------------------------------------------------


def test_panel_link_carries_the_users_filter():
    import os

    from coinfinder.config import get_settings

    os.environ["PUBLIC_BASE_URL"] = "https://alpha.up.railway.app"
    get_settings.cache_clear()
    from coinfinder.bot.main import panel_url

    url = panel_url(
        {
            "chains": ["base", "robinhood"],
            "min_clusters": 4,
            "trade_size_usd": 10.0,
            "max_mcap_usd": 500_000,
            "require_safe": True,
        }
    )
    assert url is not None
    assert "size=10" in url and "clusters=4" in url
    assert "chains=base%2Crobinhood" in url
    assert "maxmc=500000" in url and "safe=1" in url


def test_panel_link_is_omitted_without_a_public_address():
    import os

    from coinfinder.config import get_settings

    os.environ["PUBLIC_BASE_URL"] = ""
    get_settings.cache_clear()
    from coinfinder.bot.main import panel_url

    assert panel_url({"chains": ["base"], "min_clusters": 3}) is None
    os.environ.pop("PUBLIC_BASE_URL", None)
    get_settings.cache_clear()


# --- command menu ------------------------------------------------------


def test_every_advertised_command_has_a_handler():
    """The Telegram menu must not offer a command that does nothing."""
    import re

    from coinfinder.bot import main as bot_main

    source = __import__("pathlib").Path(bot_main.__file__).read_text()
    handled: set[str] = set()
    for match in re.finditer(r"Command\(([^)]*)\)", source):
        handled.update(re.findall(r'"([a-zA-ZğüşöçİĞÜŞÖÇ]+)"', match.group(1)))
    handled.add("start")

    for name, _ in bot_main.BOT_COMMANDS:
        assert name in handled, f"/{name} advertised but not handled"


def test_command_descriptions_fit_telegram_limits():
    from coinfinder.bot.main import BOT_COMMANDS

    for name, desc in BOT_COMMANDS:
        assert 1 <= len(name) <= 32 and name.islower()
        assert 3 <= len(desc) <= 256
