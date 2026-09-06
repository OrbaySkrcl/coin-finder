"""Alert rendering, in Turkish.

Layout follows the conventions traders already read at a glance, with four
deliberate departures from the products this is modelled on:

* **A probability, not stars.** "2x olasılığı %34" can be checked against what
  happened. A three-star rating cannot, so it can never be wrong, so it means
  nothing.
* **Independent buyers, not addresses.** "5 cüzdan → 3 bağımsız" makes sybil
  collapse visible instead of quietly inflating conviction.
* **The round trip priced at the reader's own position size.** A 21% Liq/MC
  ratio is abstract. "$10 için gidiş-dönüş %1,1, başabaş 1,011x" is the number
  that decides the trade - and because gas is charged per transaction rather
  than per dollar, it is a completely different number for a $10 trader than
  for a $500 one.
* **An explicit note that token tax is not included.** It cannot be simulated
  on free RPC, and at small sizes it is the single largest cost. Saying so is
  better than letting the reader assume the figure is complete.
"""

from __future__ import annotations

import html
from typing import Any

from coinfinder.backtest.costs import CostModel, round_trip_cost_pct
from coinfinder.chains import Chain
from coinfinder.humanize import duration, pct, tr_num, usd, usd_price

DEFAULT_TRADE_SIZE_USD = 100.0

TRADE_BOTS = (
    ("Trojan", "https://t.me/solana_trojanbot?start=r-{token}"),
    ("Maestro", "https://t.me/maestro?start={token}"),
    ("BananaGun", "https://t.me/BananaGunSniper_bot?start={token}"),
)


__all__ = [
    "DEFAULT_TRADE_SIZE_USD",
    "duration",
    "format_signal",
    "format_stats",
    "pct",
    "signal_links",
    "tr_num",
    "trade_economics",
    "usd",
    "usd_price",
]


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


SAFETY_LINE = {
    "safe": "🟢 Kontroller geçti",
    "caution": "🟡 Dikkat",
    "danger": "🔴 Engellendi",
    "unknown": "⚪ Doğrulanamadı",
}

FLAG_TEXT = {
    "no_risk_tooling_on_chain": "bu zincirde risk taraması yok — kendin araştır",
    "no_sells_despite_many_buys": "çok alım, sıfır satım — honeypot işareti",
    "liquidity_below_floor": "likidite alt sınırın altında",
    "liquidity_under_2pct_of_mcap": "likidite, market cap'in %2'sinin altında",
    "lp_not_burned": "LP yakılmamış",
    "owner_not_renounced": "sahiplik bırakılmamış",
    "very_new_token": "10 dakikadan yeni",
    "sell_activity_unknown": "satış aktivitesi bilinmiyor",
}


def break_even_multiple(cost_pct: float) -> float:
    """Gross multiple needed just to get the money back."""
    keep = 1.0 - cost_pct / 100.0
    return float("inf") if keep <= 0 else 1.0 / keep


def cost_model_for(chain: Chain, *, dex_fee_bps: int = 30) -> CostModel:
    return CostModel(dex_fee_bps=dex_fee_bps, gas_usd_per_swap=chain.typical_swap_gas_usd)


def trade_economics(
    *, liquidity_usd: float | None, chain: Chain, trade_size_usd: float
) -> tuple[float, float]:
    """Return ``(round-trip cost %, break-even multiple)`` for one position."""
    cost_pct = round_trip_cost_pct(
        size_usd=trade_size_usd,
        entry_liquidity_usd=liquidity_usd,
        model=cost_model_for(chain),
    )
    return cost_pct, break_even_multiple(cost_pct)


def format_signal(
    signal: dict[str, Any],
    chain: Chain,
    *,
    trade_size_usd: float = DEFAULT_TRADE_SIZE_USD,
) -> str:
    """Render one signal as Telegram HTML, priced for this reader."""
    token = str(signal["token"])
    symbol = esc(signal.get("symbol") or "BİLİNMİYOR")

    liq = _f(signal.get("snap_liquidity_usd"))
    mcap = _f(signal.get("snap_mcap_usd"))
    price = _f(signal.get("snap_price_usd"))
    wallets = int(signal.get("distinct_wallets") or 0)
    clusters = int(signal.get("distinct_clusters") or 0)
    verdict = str(signal.get("safety_verdict") or "unknown")
    p2x = _f(signal.get("quality_p2x"))

    lines: list[str] = [
        f"🟢 <b>ALIM SİNYALİ</b> · #{esc(chain.name.replace(' ', ''))}",
        "",
        f"💎 <b>{symbol}</b>",
    ]

    # Showing both numbers is the point: the gap is sybil collapse.
    if wallets != clusters:
        lines.append(f"🧠 Akıllı cüzdan: <b>{wallets}</b> → <b>{clusters} bağımsız</b>")
    else:
        lines.append(f"🧠 Bağımsız akıllı cüzdan: <b>{clusters}</b>")

    lines += [f"💰 Market cap: {usd(mcap)}", f"💧 Likidite: {usd(liq)}"]
    if liq and mcap:
        lines.append(f"📊 Likidite/MC: {pct(100 * liq / mcap)}")
    lines += [
        f"🕐 Yaş: {duration(signal.get('snap_age_minutes'))}",
        f"💵 Fiyat: {usd_price(price)}",
    ]
    if signal.get("usd_spent"):
        lines.append(f"🛒 Akıllı para girişi: {usd(_f(signal['usd_spent']))}")

    # --- the honest block ----------------------------------------------
    lines.append("")
    if p2x is not None:
        lines.append(f"🎯 2x'e ulaşma olasılığı: <b>{pct(p2x * 100, 0)}</b>")

    cost_pct, break_even = trade_economics(
        liquidity_usd=liq, chain=chain, trade_size_usd=trade_size_usd
    )
    if cost_pct >= 99.0:
        lines.append(f"🚨 <b>{usd(trade_size_usd)} ile çıkılamaz</b> — havuz çok sığ")
    else:
        lines.append(
            f"💸 {usd(trade_size_usd)} için gidiş-dönüş: <b>{pct(cost_pct, 1)}</b> "
            f"→ başabaş <b>{tr_num(break_even, 3)}x</b>"
        )
        # Gas is charged per transaction, so on a small position it can be the
        # dominant cost. Saying which part dominates tells the reader whether
        # the fix is a different chain or a different size.
        gas_share = 100.0 * 2.0 * chain.typical_swap_gas_usd / max(trade_size_usd, 1e-9)
        if cost_pct > 0 and gas_share / cost_pct > 0.5:
            lines.append(
                f"   ⛽ bunun {pct(gas_share, 1)} puanı gaz — bu boyutta {esc(chain.name)} pahalı"
            )
    lines.append("   ℹ️ token vergisi bu hesaba dahil değil (ölçülemiyor)")

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
        ("📈 Grafik", f"https://dexscreener.com/{chain.dexscreener_slug}/{pair}"),
        ("🔍 Explorer", f"{chain.explorer}/token/{token}"),
        ("🐦 X'te ara", f"https://x.com/search?q={token}"),
    ]
    links += [(name, url.format(token=token)) for name, url in TRADE_BOTS]
    return links


BUCKET_TR = {
    "flat (<1x)": "zararda (<1x)",
    "1-2x": "1-2x",
    "2-5x": "2-5x",
    "5-10x": "5-10x",
    "10x+": "10x+",
}

EXIT_TR = {
    "tp_2x": "2x'te sat",
    "tp_3x": "3x'te sat",
    "tp_5x": "5x'te sat",
    "ladder": "kademeli sat",
    "time_1h": "1 saat sonra sat",
    "time_4h": "4 saat sonra sat",
    "time_24h": "24 saat sonra sat",
    "hold_to_now": "hiç satma",
    "trail_35pct": "zirveden %35 düşünce sat",
    "peak_50pct": "zirvenin yarısında sat",
}


def translate_warning(text: str) -> str:
    if text.startswith("This exit model needs hindsight"):
        return (
            "Bu çıkış kuralı geleceği bilmeyi gerektiriyor. Ulaşılabilir bir sonuç "
            "değil, bir tavan olarak oku."
        )
    if text.startswith("Only "):
        count = text.split()[1]
        return (
            f"Sadece {count} sinyal eşleşti. Bu örneklemde güven aralığı çok geniştir — "
            "tek sayıya değil aralığa bak."
        )
    if text.startswith("No signals matched"):
        return "Bu filtreye uyan sinyal yok."
    return text


def format_stats(result: Any, *, filter_label: str, window_days: int, trade_size_usd: float) -> str:
    """Render a backtest result for /stats. Always shows the interval."""
    if result.signals == 0:
        return (
            f"<b>Senin filtren, son {window_days} gün</b>\n"
            f"<code>{esc(filter_label)}</code>\n\nBu filtreye uyan sinyal yok."
        )

    lines = [
        f"<b>Senin filtren, son {window_days} gün</b>",
        f"<code>{esc(filter_label)}</code>",
        f"Çıkış kuralı: <b>{esc(EXIT_TR.get(result.exit_model, result.exit_model))}</b>",
        f"İşlem başına {usd(trade_size_usd)} · ücret, kayma ve gaz düşülmüş",
        "",
        f"Sinyal: <b>{result.signals}</b>",
    ]
    if result.win_rate is not None:
        interval = (
            f" (%95 aralık {pct(result.win_rate_ci[0] * 100, 0)}–"
            f"{pct(result.win_rate_ci[1] * 100, 0)})"
            if result.win_rate_ci
            else ""
        )
        lines.append(f"Kazanç oranı: <b>{pct(result.win_rate * 100)}</b>{interval}")
    if result.median_net_multiple is not None:
        interval = (
            f" (%95 aralık {tr_num(result.median_ci[0])}–{tr_num(result.median_ci[1])})"
            if result.median_ci
            else ""
        )
        lines.append(f"Medyan: <b>{tr_num(result.median_net_multiple)}x</b>{interval}")
    if result.roi_pct is not None:
        sign = "+" if result.roi_pct >= 0 else "-"
        lines.append(
            f"Getiri: <b>{sign}{pct(abs(result.roi_pct))}</b> · "
            f"{usd(result.invested_usd)} yatırıma {usd(result.total_pnl_usd)}"
        )
    if result.dead_share is not None:
        lines.append(f"Sıfırlanan token: <b>{pct(result.dead_share * 100, 0)}</b>")
    if result.median_round_trip_cost_pct is not None:
        lines.append(f"Gidiş-dönüş maliyet: <b>{pct(result.median_round_trip_cost_pct)}</b>")

    if result.buckets:
        lines += ["", "<b>Sonuç dağılımı</b>"]
        total = sum(result.buckets.values()) or 1
        for name, count in result.buckets.items():
            bar = "▪" * max(0, round(20 * count / total))
            lines.append(f"{BUCKET_TR.get(name, name):<14} {count:>5}  {bar}")

    for warning in result.warnings:
        lines += ["", f"⚠️ {esc(translate_warning(warning))}"]
    return "\n".join(lines)
