from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coinfinder.scoring.clustering import UnionFind, build_clusters
from coinfinder.scoring.pnl import RoundTrip
from coinfinder.scoring.wallet_score import rank_wallets, score_wallet, shrunk_win_rate

NOW = datetime(2026, 9, 5, tzinfo=UTC)


def rt(wallet, token, cost, proceeds, days_ago=1, hold_minutes=120):
    closed = NOW - timedelta(days=days_ago)
    return RoundTrip(
        wallet=wallet,
        token=token,
        cost_usd=cost,
        proceeds_usd=proceeds,
        opened_at=closed - timedelta(minutes=hold_minutes),
        closed_at=closed,
    )


def good_trader(wallet="0xgood", n=12, days_ago=2):
    # Alternating 3x winners and 0.6x losers across distinct tokens.
    return [
        rt(wallet, f"0xtok{i}", 100.0, 300.0 if i % 2 == 0 else 60.0, days_ago=days_ago)
        for i in range(n)
    ]


# --- shrinkage ---------------------------------------------------------


def test_shrinkage_pulls_small_samples_toward_the_prior():
    # 3/3 must not read as 100%.
    assert shrunk_win_rate(3, 3) == pytest.approx(5 / 8)
    # A long record moves closer to its raw rate.
    assert shrunk_win_rate(80, 100) == pytest.approx(82 / 105)
    assert shrunk_win_rate(0, 0) == pytest.approx(0.4)  # prior mean


def test_short_record_cannot_max_out_the_win_term():
    # 8/8 is a raw 100% win rate. Shrinkage must keep it well under the 0.65
    # saturation point that the win term is scaled against, so a short record
    # can never earn the full win component on luck.
    lucky = score_wallet("0xlucky", [rt("0xlucky", f"0xt{i}", 100, 500) for i in range(8)], now=NOW)
    assert lucky.win_rate == 1.0
    assert lucky.shrunk_win_rate < 0.80


def test_more_trades_beat_fewer_at_identical_per_trade_quality():
    # Same alternating 3x / 0.6x pattern, same recency: the only difference is
    # sample size and breadth, and the longer record must rank higher.
    short = score_wallet("0xshort", good_trader("0xshort", n=8), now=NOW)
    long_ = score_wallet("0xlong", good_trader("0xlong", n=40), now=NOW)
    assert short.is_smart and long_.is_smart
    assert long_.score > short.score


# --- exclusions --------------------------------------------------------


def test_too_few_trades_excluded():
    s = score_wallet("0xa", good_trader(n=4), now=NOW)
    assert not s.is_smart and s.excluded_reason == "too_few_trades"


def test_too_few_distinct_tokens_excluded():
    trips = [rt("0xa", "0xsame", 100, 300) for _ in range(12)]
    s = score_wallet("0xa", trips, now=NOW)
    assert not s.is_smart and s.excluded_reason == "too_few_tokens"


def test_sniper_bot_excluded_on_hold_time():
    trips = [rt("0xbot", f"0xt{i}", 100, 300, hold_minutes=0.5) for i in range(12)]
    s = score_wallet("0xbot", trips, now=NOW)
    assert not s.is_smart and s.excluded_reason == "bot_like_hold"


def test_unprofitable_wallet_excluded():
    trips = [rt("0xbad", f"0xt{i}", 100, 40) for i in range(12)]
    s = score_wallet("0xbad", trips, now=NOW)
    assert not s.is_smart and s.excluded_reason == "not_profitable"


def test_high_frequency_bot_excluded():
    # 400 round trips across a 4-day observable span = 100/day.
    trips = [
        rt("0xhf", f"0xt{i}", 100, 300, days_ago=1 + (i % 4), hold_minutes=30) for i in range(400)
    ]
    s = score_wallet("0xhf", trips, now=NOW)
    assert not s.is_smart and s.excluded_reason == "bot_like_frequency"


def test_burst_inside_a_short_window_is_not_called_a_bot():
    # A wallet we have only observed for one afternoon must not be excluded on
    # frequency - there is not enough history to judge it yet.
    trips = [rt("0xnew", f"0xt{i}", 100, 300, days_ago=1, hold_minutes=60) for i in range(30)]
    s = score_wallet("0xnew", trips, now=NOW)
    assert s.is_smart and s.excluded_reason is None


# --- decay -------------------------------------------------------------


def test_recent_performance_outranks_identical_stale_performance():
    fresh = score_wallet("0xfresh", good_trader("0xfresh", days_ago=2), now=NOW)
    stale = score_wallet("0xstale", good_trader("0xstale", days_ago=120), now=NOW)
    assert fresh.is_smart and fresh.score > stale.score
    assert fresh.weighted_pnl_usd > stale.weighted_pnl_usd
    # Raw PnL is identical: only the decay differs.
    assert fresh.realized_pnl_usd == pytest.approx(stale.realized_pnl_usd)


def test_one_huge_win_saturates_instead_of_exploding_the_score():
    modest = good_trader("0xwhale", n=12)
    whale = [*modest, rt("0xwhale", "0xmoon", 100, 5_000_000)]
    a = score_wallet("0xwhale", whale, now=NOW)
    b = score_wallet("0xwhale", modest, now=NOW)
    assert a.is_smart and b.is_smart
    # A 50,000x outlier adds ~1500x the PnL but the score is capped at 100 and
    # rises by less than 40 points, so it cannot single-handedly own the top.
    assert a.weighted_pnl_usd > b.weighted_pnl_usd * 100
    assert a.score <= 100.0
    assert a.score - b.score < 40.0


def test_rank_wallets_orders_and_truncates():
    ranked = rank_wallets(
        {
            "0xa": good_trader("0xa", n=30),
            "0xb": good_trader("0xb", n=12),
            "0xc": good_trader("0xc", n=3),  # ineligible
        },
        now=NOW,
        top_n=2,
    )
    smart = [s for s in ranked if s.is_smart]
    assert [s.wallet for s in smart] == ["0xa", "0xb"]
    assert any(s.wallet == "0xc" and not s.is_smart for s in ranked)


# --- clustering --------------------------------------------------------


def test_unionfind_groups_transitively():
    uf = UnionFind()
    uf.union("b", "c")
    uf.union("a", "b")
    assert uf.find("c") == uf.find("a") == "a"
    assert uf.groups() == {"a": ["a", "b", "c"]}


def buy(wallet, token, second):
    return {"wallet": wallet, "token": token, "ts": NOW + timedelta(seconds=second)}


def test_coordinated_wallets_are_merged():
    # Two wallets buying the same four tokens seconds apart = one operator.
    buys = []
    for i, tok in enumerate(["0x1", "0x2", "0x3", "0x4"]):
        buys += [buy("0xsyb1", tok, i * 1000), buy("0xsyb2", tok, i * 1000 + 10)]
    result = build_clusters(buys)
    assert result.cluster_of("0xsyb1") == result.cluster_of("0xsyb2")
    assert result.distinct_clusters(["0xsyb1", "0xsyb2"]) == 1


def test_independent_traders_are_not_merged():
    # Both active, overlapping on only two tokens out of ten each.
    buys = []
    for i in range(10):
        buys.append(buy("0xind1", f"0xa{i}", i * 5000))
        buys.append(buy("0xind2", f"0xb{i}", i * 5000))
    for i, tok in enumerate(["0xshared1", "0xshared2"]):
        buys += [buy("0xind1", tok, 90000 + i * 1000), buy("0xind2", tok, 90000 + i * 1000 + 20)]
    result = build_clusters(buys)
    assert result.cluster_of("0xind1") != result.cluster_of("0xind2")
    assert result.distinct_clusters(["0xind1", "0xind2"]) == 2


def test_same_token_far_apart_is_not_coordination():
    buys = []
    for i, tok in enumerate(["0x1", "0x2", "0x3", "0x4"]):
        # One hour apart: independent discovery, not one operator.
        buys += [buy("0xw1", tok, i * 10000), buy("0xw2", tok, i * 10000 + 3600)]
    result = build_clusters(buys)
    assert result.cluster_of("0xw1") != result.cluster_of("0xw2")


def test_cluster_collapses_conviction_count():
    buys = []
    for i, tok in enumerate(["0x1", "0x2", "0x3", "0x4"]):
        for w in ("0xs1", "0xs2", "0xs3"):
            buys.append(buy(w, tok, i * 1000 + int(w[-1]) * 5))
    result = build_clusters(buys)
    # Three wallets, one operator: conviction is 1, not 3.
    assert result.distinct_clusters(["0xs1", "0xs2", "0xs3"]) == 1
    assert len(result.multi_wallet_clusters) == 1
