"""Tests for the plain-language diagnostics a non-technical operator reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coinfinder.config import Settings
from coinfinder.diagnostics import Check, Level, Report, build_progress, headline_for

SETTINGS = Settings(smart_wallet_min_trades=8, confluence_min_clusters=3, telegram_bot_token="x")
NOW = datetime.now(UTC)


def counts(**over):
    base = {
        "candidate_wallets": 0,
        "watched_wallets": 0,
        "trades": 0,
        "smart_wallets": 0,
        "signals": 0,
        "signals_24h": 0,
        "last_trade_at": None,
        "last_signal_at": None,
        "first_wallet_at": None,
    }
    base.update(over)
    return base


# --- progress ----------------------------------------------------------


def test_fresh_install_shows_the_first_step_as_active():
    steps = build_progress(counts(), SETTINGS)
    assert [s["done"] for s in steps] == [False] * 4
    assert [s["active"] for s in steps] == [True, False, False, False]


def test_progress_advances_with_the_data():
    steps = build_progress(counts(candidate_wallets=120, trades=4000), SETTINGS)
    assert steps[0]["done"] and steps[1]["done"]
    assert steps[2]["active"] is True
    assert "120" in steps[0]["detail"] and "4,000" in steps[1]["detail"]


def test_a_gap_behind_a_finished_step_is_not_shown_as_pending():
    # Regression: an empty step sitting behind completed ones used to read
    # "starts after wallets are found", which looks like a fault when the
    # later steps have plainly already run.
    steps = build_progress(counts(candidate_wallets=120, smart_wallets=90, signals=1500), SETTINGS)
    gap = steps[1]
    assert not gap["done"] and not gap["active"]
    assert gap["detail"] == "Bu adımda kayıt yok."


def test_pending_steps_explain_their_own_requirement():
    steps = build_progress(counts(candidate_wallets=10), SETTINGS)
    assert "8" in steps[2]["detail"]  # minimum closed trades
    assert "3" in steps[3]["detail"]  # minimum independent wallets


def test_all_complete():
    steps = build_progress(
        counts(candidate_wallets=200, trades=9000, smart_wallets=90, signals=1500, signals_24h=77),
        SETTINGS,
    )
    assert all(s["done"] for s in steps)
    assert not any(s["active"] for s in steps)


# --- headline ----------------------------------------------------------


def test_headline_reports_a_hard_failure_first():
    text, level = headline_for(build_progress(counts(), SETTINGS), counts(), Level.ERROR)
    assert level is Level.ERROR and "sorun" in text


def test_headline_for_a_brand_new_install_is_reassuring():
    c = counts(first_wallet_at=NOW - timedelta(minutes=10))
    text, level = headline_for(build_progress(c, SETTINGS), c, Level.OK)
    assert level is Level.WAITING and "normal" in text


def test_headline_during_warm_up_gives_an_expectation():
    c = counts(candidate_wallets=50, first_wallet_at=NOW - timedelta(hours=20))
    text, level = headline_for(build_progress(c, SETTINGS), c, Level.OK)
    assert level is Level.WAITING and "2-4 gün" in text


def test_headline_flags_a_warm_up_that_is_taking_too_long():
    c = counts(candidate_wallets=50, first_wallet_at=NOW - timedelta(days=5))
    text, level = headline_for(build_progress(c, SETTINGS), c, Level.WARN)
    assert level is Level.WARN and "yavaş" in text


def test_headline_when_signals_are_flowing():
    c = counts(
        candidate_wallets=200,
        trades=9000,
        smart_wallets=90,
        signals=1500,
        last_signal_at=NOW - timedelta(hours=1),
    )
    text, level = headline_for(build_progress(c, SETTINGS), c, Level.OK)
    assert level is Level.OK and "çalışıyor" in text


def test_headline_notices_signals_have_stopped():
    c = counts(
        candidate_wallets=200,
        trades=9000,
        smart_wallets=90,
        signals=1500,
        last_signal_at=NOW - timedelta(days=3),
    )
    text, level = headline_for(build_progress(c, SETTINGS), c, Level.WARN)
    assert level is Level.WARN and "dar" in text


# --- report shape ------------------------------------------------------


def test_worst_level_wins():
    report = Report(
        checks=[
            Check("a", Level.OK, "A", ""),
            Check("b", Level.WAITING, "B", ""),
            Check("c", Level.WARN, "C", ""),
        ]
    )
    assert report.worst is Level.WARN
    report.checks.append(Check("d", Level.ERROR, "D", ""))
    assert report.worst is Level.ERROR


def test_all_ok_reports_ok():
    assert Report(checks=[Check("a", Level.OK, "A", "")]).worst is Level.OK


def test_report_serialises_for_the_dashboard():
    report = Report(
        checks=[Check("db", Level.ERROR, "Veritabanı", "Bağlanamıyorum.")],
        headline="Bir sorun var",
        headline_level=Level.ERROR,
        progress=build_progress(counts(), SETTINGS),
    )
    payload = report.to_dict()
    assert payload["headline_level"] == "error"
    assert payload["headline_icon"] == "❌"
    assert payload["checks"][0]["code"] == "db"
    assert payload["checks"][0]["icon"] == "❌"
    assert len(payload["progress"]) == 4
    assert "generated_at" in payload


def test_telegram_rendering_includes_every_check_and_step():
    report = Report(
        checks=[Check("db", Level.OK, "Veritabanı", "Bağlı.")],
        headline="Her şey çalışıyor",
        headline_level=Level.OK,
        progress=build_progress(counts(candidate_wallets=5), SETTINGS),
    )
    text = report.as_telegram()
    assert "Her şey çalışıyor" in text
    assert "Veritabanı" in text
    assert "Kurulum ilerlemesi" in text
    assert text.count("\n") > 5


@pytest.mark.parametrize("level", list(Level))
def test_every_level_has_an_icon(level):
    from coinfinder.diagnostics import ICON

    assert ICON[level]
