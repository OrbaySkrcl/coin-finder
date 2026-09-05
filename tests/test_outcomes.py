from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coinfinder.ingest.outcomes import compute_update

T0 = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def signal(**over):
    row = {
        "id": 1,
        "token": "0xtok",
        "ts": T0,
        "snap_price_usd": 0.001,
        "peak_multiple": None,
        "mult_15m": None,
        "mult_1h": None,
        "mult_4h": None,
        "mult_24h": None,
        "mult_7d": None,
    }
    row.update(over)
    return row


def test_multiple_is_price_over_entry():
    u = compute_update(
        signal(), price_usd=0.003, liquidity_usd=50_000, listed=True, now=T0 + timedelta(hours=2)
    )
    assert u.current_multiple == pytest.approx(3.0)
    assert u.peak_multiple == pytest.approx(3.0)
    assert not u.is_dead


def test_delisted_token_is_dead_and_worth_zero():
    # Survivorship: a rug must stay in the sample at -100%, not vanish.
    u = compute_update(signal(), price_usd=None, liquidity_usd=None, listed=False, now=T0)
    assert u.is_dead and u.current_multiple == 0.0


def test_drained_liquidity_counts_as_dead():
    u = compute_update(
        signal(), price_usd=0.002, liquidity_usd=100.0, listed=True, now=T0 + timedelta(hours=1)
    )
    assert u.is_dead and u.current_multiple == 0.0


def test_peak_never_decreases():
    u = compute_update(
        signal(peak_multiple=9.0),
        price_usd=0.0005,
        liquidity_usd=50_000,
        listed=True,
        now=T0 + timedelta(hours=5),
    )
    assert u.current_multiple == pytest.approx(0.5)
    assert u.peak_multiple == pytest.approx(9.0)


def test_horizons_fill_only_once_they_have_passed():
    u = compute_update(
        signal(), price_usd=0.002, liquidity_usd=50_000, listed=True, now=T0 + timedelta(minutes=30)
    )
    assert set(u.horizons) == {"15m"}


def test_already_recorded_horizon_is_not_rewritten():
    # The value a 1-hour stop would have returned is fixed at that hour.
    u = compute_update(
        signal(mult_15m=1.4, mult_1h=2.0),
        price_usd=0.0001,
        liquidity_usd=50_000,
        listed=True,
        now=T0 + timedelta(hours=6),
    )
    assert "15m" not in u.horizons and "1h" not in u.horizons
    assert set(u.horizons) == {"4h"}


def test_all_horizons_fill_for_an_old_signal():
    u = compute_update(
        signal(), price_usd=0.001, liquidity_usd=50_000, listed=True, now=T0 + timedelta(days=10)
    )
    assert set(u.horizons) == {"15m", "1h", "4h", "24h", "7d"}


def test_dead_signal_records_zero_at_its_horizons():
    u = compute_update(
        signal(), price_usd=None, liquidity_usd=None, listed=False, now=T0 + timedelta(hours=2)
    )
    assert u.horizons == {"15m": 0.0, "1h": 0.0}
