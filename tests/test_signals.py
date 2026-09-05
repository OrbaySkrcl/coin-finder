from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coinfinder.chains import BASE, ROBINHOOD
from coinfinder.scoring.clustering import ClusterResult
from coinfinder.signals import quality, safety
from coinfinder.signals.engine import (
    build_signal_payload,
    cooldown_bucket,
    dedupe_key,
    detect_confluence,
    passes_entry_filters,
)
from coinfinder.sources.dexscreener import parse_pair

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
NO_CLUSTERS = ClusterResult(mapping={}, multi_wallet_clusters={})


def buy(wallet, token, minutes_ago, usd=100.0):
    return {
        "wallet": wallet,
        "token": token,
        "ts": NOW - timedelta(minutes=minutes_ago),
        "usd_value": usd,
    }


# --- confluence --------------------------------------------------------


def test_three_independent_wallets_produce_a_signal():
    buys = [buy(f"0xw{i}", "0xtok", 10 + i) for i in range(3)]
    (c,) = detect_confluence(
        buys, clusters=NO_CLUSTERS, now=NOW, window_minutes=180, min_clusters=3
    )
    assert c.distinct_wallets == 3 and c.distinct_clusters == 3
    assert c.usd_spent == pytest.approx(300.0)


def test_two_wallets_do_not_reach_the_threshold():
    buys = [buy(f"0xw{i}", "0xtok", 10) for i in range(2)]
    assert (
        detect_confluence(buys, clusters=NO_CLUSTERS, now=NOW, window_minutes=180, min_clusters=3)
        == []
    )


def test_sybil_wallets_collapse_below_the_threshold():
    # Five addresses, but three of them are one operator -> 3 clusters, not 5.
    clusters = ClusterResult(
        mapping={"0xw0": "0xw0", "0xw1": "0xw0", "0xw2": "0xw0"}, multi_wallet_clusters={}
    )
    buys = [buy(f"0xw{i}", "0xtok", 10) for i in range(3)]
    assert (
        detect_confluence(buys, clusters=clusters, now=NOW, window_minutes=180, min_clusters=3)
        == []
    )


def test_one_wallet_buying_repeatedly_is_not_confluence():
    buys = [buy("0xsame", "0xtok", m) for m in (5, 10, 15, 20)]
    assert (
        detect_confluence(buys, clusters=NO_CLUSTERS, now=NOW, window_minutes=180, min_clusters=3)
        == []
    )


def test_buys_outside_the_window_are_dropped():
    buys = [buy("0xa", "0xtok", 5), buy("0xb", "0xtok", 10), buy("0xc", "0xtok", 400)]
    assert (
        detect_confluence(buys, clusters=NO_CLUSTERS, now=NOW, window_minutes=180, min_clusters=3)
        == []
    )


def test_tokens_are_ranked_by_conviction():
    buys = [buy(f"0xa{i}", "0xweak", 5) for i in range(3)]
    buys += [buy(f"0xb{i}", "0xstrong", 5) for i in range(6)]
    results = detect_confluence(
        buys, clusters=NO_CLUSTERS, now=NOW, window_minutes=180, min_clusters=3
    )
    assert [c.token for c in results] == ["0xstrong", "0xweak"]


# --- dedupe ------------------------------------------------------------


def test_same_conviction_in_one_bucket_dedupes():
    b = cooldown_bucket(NOW, 360)
    assert dedupe_key(8453, "0xT", 3, b) == dedupe_key(8453, "0xt", 3, b)


def test_rising_conviction_produces_a_new_key():
    b = cooldown_bucket(NOW, 360)
    assert dedupe_key(8453, "0xt", 3, b) != dedupe_key(8453, "0xt", 5, b)


def test_cooldown_bucket_floors_onto_a_grid():
    a = cooldown_bucket(datetime(2026, 9, 5, 12, 5, tzinfo=UTC), 360)
    b = cooldown_bucket(datetime(2026, 9, 5, 14, 59, tzinfo=UTC), 360)
    assert a == b
    assert cooldown_bucket(datetime(2026, 9, 5, 18, 1, tzinfo=UTC), 360) != a


# --- safety ------------------------------------------------------------


def base_kwargs(**over):
    kwargs = dict(
        chain=BASE,
        sells_observed=40,
        buys_observed=100,
        liquidity_usd=50_000.0,
        mcap_usd=200_000.0,
        lp_burned_pct=100.0,
        owner_renounced=True,
        min_liquidity_usd=5_000.0,
        age_minutes=120,
    )
    kwargs.update(over)
    return kwargs


def test_clean_token_is_safe():
    r = safety.assess(**base_kwargs())
    assert r.verdict is safety.Verdict.SAFE and r.flags == []


def test_buys_without_sells_is_flagged_as_a_honeypot():
    r = safety.assess(**base_kwargs(sells_observed=0, buys_observed=40))
    assert r.verdict is safety.Verdict.DANGER
    assert "no_sells_despite_many_buys" in r.flags


def test_zero_sells_with_few_buys_is_not_yet_conclusive():
    r = safety.assess(**base_kwargs(sells_observed=0, buys_observed=3))
    assert r.verdict is not safety.Verdict.DANGER


def test_liquidity_floor_blocks():
    r = safety.assess(**base_kwargs(liquidity_usd=900.0))
    assert r.verdict is safety.Verdict.DANGER and "liquidity_below_floor" in r.flags


def test_thin_liquidity_relative_to_mcap_is_caution():
    r = safety.assess(**base_kwargs(liquidity_usd=10_000.0, mcap_usd=5_000_000.0))
    assert r.verdict is safety.Verdict.CAUTION
    assert "liquidity_under_2pct_of_mcap" in r.flags


def test_unburned_lp_and_live_owner_downgrade_to_caution():
    r = safety.assess(**base_kwargs(lp_burned_pct=0.0, owner_renounced=False))
    assert r.verdict is safety.Verdict.CAUTION
    assert {"lp_not_burned", "owner_not_renounced"} <= set(r.flags)


def test_robinhood_never_claims_safe():
    # No third-party risk tooling covers this chain, so "safe" would be a lie.
    r = safety.assess(**base_kwargs(chain=ROBINHOOD))
    assert r.verdict is safety.Verdict.CAUTION
    assert r.limited_coverage and "no_risk_tooling_on_chain" in r.flags


# --- quality -----------------------------------------------------------


def test_prior_is_marked_unfitted():
    assert quality.PRIOR.is_fitted is False


def test_deeper_liquidity_raises_probability():
    common = dict(
        mcap_usd=200_000.0,
        distinct_clusters=3,
        age_minutes=60,
        buys_24h=100,
        sells_24h=50,
        usd_spent=500.0,
        safety_verdict="safe",
    )
    thin = quality.PRIOR.predict_p2x(quality.build_features(liquidity_usd=3_000.0, **common))
    deep = quality.PRIOR.predict_p2x(quality.build_features(liquidity_usd=120_000.0, **common))
    assert deep > thin


def test_more_clusters_raise_probability_and_danger_lowers_it():
    common = dict(
        liquidity_usd=50_000.0,
        mcap_usd=200_000.0,
        age_minutes=60,
        buys_24h=100,
        sells_24h=50,
        usd_spent=500.0,
    )
    low = quality.PRIOR.predict_p2x(
        quality.build_features(distinct_clusters=3, safety_verdict="safe", **common)
    )
    high = quality.PRIOR.predict_p2x(
        quality.build_features(distinct_clusters=8, safety_verdict="safe", **common)
    )
    danger = quality.PRIOR.predict_p2x(
        quality.build_features(distinct_clusters=8, safety_verdict="danger", **common)
    )
    assert low < high and danger < low


def test_probability_stays_in_range_for_extreme_inputs():
    f = quality.build_features(
        liquidity_usd=1e12,
        mcap_usd=1e-6,
        distinct_clusters=10_000,
        age_minutes=0,
        buys_24h=10**9,
        sells_24h=0,
        usd_spent=1e12,
        safety_verdict="safe",
    )
    p = quality.PRIOR.predict_p2x(f)
    assert 0.0 <= p <= 1.0


def test_fit_falls_back_to_prior_without_enough_samples():
    assert quality.fit([{"features": {}, "reached_2x": True}]) is quality.PRIOR


def test_fit_learns_a_separating_feature():
    # Liquidity perfectly predicts the outcome in this synthetic set, so a
    # fitted model must rank the deep-liquidity case above the thin one.
    samples = []
    for i in range(500):
        deep = i % 2 == 0
        samples.append(
            {
                "features": quality.build_features(
                    liquidity_usd=200_000.0 if deep else 2_000.0,
                    mcap_usd=200_000.0,
                    distinct_clusters=3,
                    age_minutes=60,
                    buys_24h=100,
                    sells_24h=50,
                    usd_spent=500.0,
                    safety_verdict="safe",
                ),
                "reached_2x": deep,
            }
        )
    model = quality.fit(samples, epochs=200)
    assert model.is_fitted and model.trained_on == 500
    thin_f = samples[1]["features"]
    deep_f = samples[0]["features"]
    assert model.predict_p2x(deep_f) > 0.8
    assert model.predict_p2x(thin_f) < 0.2


# --- entry filters -----------------------------------------------------


PAIR = parse_pair(
    {
        "chainId": "base",
        "baseToken": {"address": "0xtok", "symbol": "T"},
        "priceUsd": "0.001",
        "liquidity": {"usd": 40_000.0},
        "marketCap": 190_000,
        "txns": {"h24": {"buys": 50, "sells": 20}},
        "pairCreatedAt": 1757000000000,
    }
)


def make_payload(pair=PAIR, verdict=safety.Verdict.SAFE):
    conf = detect_confluence(
        [buy(f"0xw{i}", "0xtok", 5) for i in range(3)],
        clusters=NO_CLUSTERS,
        now=NOW,
        window_minutes=180,
        min_clusters=3,
    )[0]
    return build_signal_payload(
        chain=BASE,
        confluence=conf,
        pair=pair,
        safety_report=safety.SafetyReport(verdict=verdict),
        model=quality.PRIOR,
        block_number=1,
        cooldown_minutes=360,
    )


def test_payload_freezes_the_snapshot():
    p = make_payload()
    assert p["snap_liquidity_usd"] == 40_000.0
    assert p["snap_mcap_usd"] == 190_000.0
    assert p["distinct_clusters"] == 3
    assert 0.0 <= p["quality_p2x"] <= 1.0
    assert p["_model_fitted"] is False


def test_entry_filters_accept_a_good_signal():
    ok, reason = passes_entry_filters(
        make_payload(), min_liquidity_usd=5_000, max_entry_mcap_usd=5_000_000
    )
    assert ok and reason is None


def test_entry_filters_reject_danger_and_thin_and_huge_and_priceless():
    assert passes_entry_filters(
        make_payload(verdict=safety.Verdict.DANGER),
        min_liquidity_usd=5_000,
        max_entry_mcap_usd=5_000_000,
    ) == (False, "safety_danger")
    assert passes_entry_filters(
        make_payload(), min_liquidity_usd=100_000, max_entry_mcap_usd=5_000_000
    ) == (False, "liquidity_below_floor")
    assert passes_entry_filters(
        make_payload(), min_liquidity_usd=5_000, max_entry_mcap_usd=1_000
    ) == (False, "mcap_above_ceiling")
    assert passes_entry_filters(
        make_payload(pair=None), min_liquidity_usd=5_000, max_entry_mcap_usd=5_000_000
    ) == (False, "no_price")
