"""Alert rendering.

Layout follows the conventions traders already read at a glance, with three
deliberate departures from the reference product:

* **A probability, not stars.** "P(2x) 34%" can be checked against outcomes.
  A three-star rating cannot, so it can never be wrong, so it means nothing.
* **Independent buyers, not addresses.** "4 wallets / 3 independent" makes
  sybil collapse visible instead of quietly inflating conviction.
* **The cost of the round trip at the reader's own size.** A 21% Liq/MC ratio
  is abstract; "$500 in this pool costs 12.3% to enter and exit, so you need
  1.14x to break even" is the number that decides the trade.
"""

from __future__ import annotations

import html
from typing import Any

from coinfinder.backtest.costs import CostModel, round_trip_cost_pct
from coinfinder.chains import Chain

TRADE_BOTS = (
    ("Trojan", "https://t.me/solana_trojanbot?start=r-{token}"),
    ("Maestro", "https://t.me/maestro?start={token}"),
    ("BananaGun", "https://t.me/BananaGunSniper_bot?start={token}"),
)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def usd(value: float | None, *, decimals: int = 0) -> str:
    if value is None:
        return "?"
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:,.1f}k"
    if value >= 1:
        return f"${value:,.{decimals}f}"
    return f"${value:.8f}".rstrip("0").rstrip(".")


def duration(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


SAFETY_LINE = {
    "safe": "🟢 Checks passed",
    "caution": "🟡 Caution",
    "danger": "🔴 Blocked",
    "unknown": "⚪ Unverified",
}

FLAG_TEXT = {
    "no_risk_tooling_on_chain": "no risk tooling on this chain — DYOR",
    "no_sells_despite_many_buys": "many buys, zero sells — honeypot pattern",
    "liquidity_below_floor": "liquidity below floor",
    "liquidity_under_2pct_of_mcap": "liquidity under 2% of mcap",
    "lp_not_burned": "LP not burned",
    "owner_not_renounced": "owner not renounced",
    "very_new_token": "under 10 minutes old",
    "sell_activity_unknown": "sell activity unknown",
}


def break_even_multiple(cost_pct: float) -> float:
    """Gross multiple needed just to get the money back."""
    keep = 1.0 - cost_pct / 100.0
    return float("inf") if keep <= 0 else 1.0 / keep


def format_signal(
    signal: dict[str, Any],
    chain: Chain,
    *,
    trade_size_usd: float = 100.0,
    cost_model: CostModel | None = None,
) -> str:
    """Render one signal as Telegram HTML."""
    cost_model = cost_model or CostModel(gas_usd_per_swap=chain.typical_swap_gas_usd)
    token = str(signal["token"])
    symbol = esc(signal.get("symbol") or "UNKNOWN")

    liq = _f(signal.get("snap_liquidity_usd"))
    mcap = _f(signal.get("snap_mcap_usd"))
    price = _f(signal.get("snap_price_usd"))
    wallets = int(signal.get("distinct_wallets") or 0)
    clusters = int(signal.get("distinct_clusters") or 0)
    verdict = str(signal.get("safety_verdict") or "unknown")
    p2x = _f(signal.get("quality_p2x"))

    lines: list[str] = [
        f"🟢 <b>BUY SIGNAL</b> · #{esc(chain.name.replace(' ', ''))}",
        "",
        f"💎 <b>{symbol}</b>",
    ]

    # Conviction. Showing both numbers is the point: the gap is sybil collapse.
    if wallets != clusters:
        lines.append(f"🧠 Smart wallets: <b>{wallets}</b> → <b>{clusters} independent</b>")
    else:
        lines.append(f"🧠 Independent smart wallets: <b>{clusters}</b>")

    lines += [
        f"💰 MCAP: {usd(mcap)}",
        f"💧 Liquidity: {usd(liq)}",
    ]
    if liq and mcap:
        lines.append(f"📊 Liq/MC: {100 * liq / mcap:.1f}%")
    lines += [
        f"🕐 Age: {duration(signal.get('snap_age_minutes'))}",
        f"💵 Price: {usd(price)}",
    ]
    if signal.get("usd_spent"):
        lines.append(f"🛒 Smart money in: {usd(_f(signal['usd_spent']))}")

    # --- the honest block ----------------------------------------------
    lines.append("")
    if p2x is not None:
        lines.append(f"🎯 P(reaches 2x): <b>{p2x:.0%}</b>")
    cost_pct = round_trip_cost_pct(
        size_usd=trade_size_usd, entry_liquidity_usd=liq, model=cost_model
    )
    if cost_pct >= 99.0:
        lines.append(f"🚨 <b>Not exitable</b> at ${trade_size_usd:,.0f} — pool too thin")
    else:
        be = break_even_multiple(cost_pct)
        lines.append(
            f"💸 Round trip at ${trade_size_usd:,.0f}: <b>{cost_pct:.1f}%</b> "
            f"→ need <b>{be:.2f}x</b> to break even"
        )

    flags = signal.get("safety_flags") or []
    lines.append(f"🛡 {SAFETY_LINE.get(verdict, SAFETY_LINE['unknown'])}")
    for flag in flags[:4]:
        lines.append(f"   • {esc(FLAG_TEXT.get(flag, flag))}")

    lines += ["", f"<code>{esc(token)}</code>"]
    return "\n".join(lines)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def signal_links(signal: dict[str, Any], chain: Chain) -> list[tuple[str, str]]:
    """Buttons for an alert: chart, explorer, and quick-buy bots."""
    token = str(signal["token"])
    pair = signal.get("pair_address") or token
    links = [
        ("📈 Chart", f"https://dexscreener.com/{chain.dexscreener_slug}/{pair}"),
        ("🔍 Explorer", f"{chain.explorer}/token/{token}"),
        ("🐦 X Search", f"https://x.com/search?q={token}"),
    ]
    links += [(name, url.format(token=token)) for name, url in TRADE_BOTS]
    return links


def format_stats(result: Any, *, filter_label: str, window_days: int) -> str:
    """Render a backtest result for /stats. Always shows the interval."""
    if result.signals == 0:
        return (
            f"<b>Your filter, last {window_days} days</b>\n"
            f"<code>{esc(filter_label)}</code>\n\nNo signals matched."
        )

    lines = [
        f"<b>Your filter, last {window_days} days</b>",
        f"<code>{esc(filter_label)}</code>",
        f"Exit rule: <b>{esc(result.exit_model)}</b> · net of fees, slippage and gas",
        "",
        f"Signals: <b>{result.signals}</b>",
    ]
    if result.win_rate is not None:
        ci = (
            f" (95% CI {result.win_rate_ci[0]:.0%}–{result.win_rate_ci[1]:.0%})"
            if result.win_rate_ci
            else ""
        )
        lines.append(f"Win rate: <b>{result.win_rate:.1%}</b>{ci}")
    if result.median_net_multiple is not None:
        ci = (
            f" (95% CI {result.median_ci[0]:.2f}–{result.median_ci[1]:.2f})"
            if result.median_ci
            else ""
        )
        lines.append(f"Median: <b>{result.median_net_multiple:.2f}x</b>{ci}")
    if result.roi_pct is not None:
        lines.append(f"ROI: <b>{result.roi_pct:+.1f}%</b> on {usd(result.invested_usd)}")
    if result.dead_share is not None:
        lines.append(f"Tokens that died: <b>{result.dead_share:.0%}</b>")
    if result.median_round_trip_cost_pct is not None:
        lines.append(f"Median round-trip cost: <b>{result.median_round_trip_cost_pct:.1f}%</b>")

    if result.buckets:
        lines += ["", "<b>Outcome spread</b>"]
        total = sum(result.buckets.values()) or 1
        for name, count in result.buckets.items():
            bar = "▪" * max(0, round(20 * count / total))
            lines.append(f"{name:<10} {count:>5}  {bar}")

    for warning in result.warnings:
        lines += ["", f"⚠️ {esc(warning)}"]
    return "\n".join(lines)
