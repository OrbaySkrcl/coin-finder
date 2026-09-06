"""Telegram bot: alerts, per-user filters, and honest performance reporting.

All user-facing text is Turkish. Everything that crosses a boundary - callback
payloads, filter column names, exit-model identifiers - stays English so the
data layer remains language-independent.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import structlog
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from coinfinder import db, diagnostics, repo
from coinfinder.backtest.costs import CostModel
from coinfinder.backtest.engine import FilterSpec, run
from coinfinder.backtest.exits import by_name
from coinfinder.bot.format import (
    DEFAULT_TRADE_SIZE_USD,
    format_signal,
    format_stats,
    signal_links,
    tr_num,
    trade_economics,
    usd,
)
from coinfinder.chains import ALL_CHAINS, BY_CHAIN_ID, get_chain
from coinfinder.config import get_settings
from coinfinder.humanize import unwrap
from coinfinder.logging_setup import setup_logging

log = structlog.get_logger(__name__)
router = Router()

ALERT_POLL_SECONDS = 15
STATS_WINDOW_DAYS = 30
STATS_EXIT_MODEL = "ladder"

#: Offered position sizes. Small values first: gas is charged per transaction,
#: so it is small positions whose economics change most between chains, and
#: those are the users the sizing control exists for.
SIZE_OPTIONS = (5, 10, 20, 50, 100, 250)
#: Round-trip cost ceilings, as a percentage of position size. 0 disables.
COST_OPTIONS = (2, 5, 10, 0)

# Source stays wrapped for readability; unwrap() joins each paragraph into one
# line so Telegram can wrap it to the reader's screen instead.
WELCOME = (
    unwrap("""<b>Alpha Coin Finder</b> — akıllı para sinyalleri

Base, Robinhood Chain ve BNB Chain'de geçmişte kâr etmiş cüzdanları izliyorum.
Bunlardan birkaç <i>bağımsız</i> tanesi aynı tokeni alınca sana haber veriyorum.

<b>Farkı şurada:</b>

• Kanaat, adres sayısını değil <b>bağımsız kişi sayısını</b> sayar. Aynı
kaynaktan beslenen beş cüzdan bir kişidir, beş değil.

• Kalite bir yıldız değil, <b>kontrol edebileceğin bir olasılık</b>.

• Her sinyalde <b>senin pozisyon boyutunda</b> gidiş-dönüş maliyeti ve başabaş
çarpanı yazıyor. Gaz işlem başına alınır, o yüzden bu sayı $10 ile $500
arasında tamamen değişir.

• /karne senin filtreni gerçek geçmişe karşı test eder — ücret, kayma ve gaz
düşülmüş, güven aralığıyla birlikte.

Bunlar veridir, yatırım tavsiyesi değildir. Bu piyasadaki tokenlerin çoğu
sıfırlanır.""")
    + """

<b>Komutlar</b>
/ayarlar — sana ne ulaşacağını belirle
/karne — filtrenin gerçekte ne kazandırdığı
/durum — sistem çalışıyor mu
/top — en yüksek puanlı cüzdanlar
/durdur — bildirimleri durdur"""
)


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚙️ Ayarlar", callback_data="menu:filters"),
                InlineKeyboardButton(text="📊 Karnem", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton(text="🏆 En iyi cüzdanlar", callback_data="menu:top"),
                InlineKeyboardButton(text="🩺 Sistem durumu", callback_data="menu:status"),
            ],
        ]
    )


def user_size(user: dict[str, Any]) -> float:
    raw = user.get("trade_size_usd")
    try:
        return float(raw) if raw else DEFAULT_TRADE_SIZE_USD
    except (TypeError, ValueError):
        return DEFAULT_TRADE_SIZE_USD


def _filters_keyboard(user: dict[str, Any]) -> InlineKeyboardMarkup:
    """Buttons only. The message above carries the labels.

    Telegram truncates button text to fit, and at four buttons to a row a
    phone allows roughly nine characters. "○ maliyet ≤%2" became "○ maliyet ≤"
    for every option, making the control unusable, so the labels here are kept
    terse and _filters_summary above provides the legend.
    """
    chains = set(user.get("chains") or [])
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if key in chains else '⬜'} {chain.short_name}",
                callback_data=f"chain:{key}",
            )
            for key, chain in ALL_CHAINS.items()
        ]
    ]

    current_clusters = int(user.get("min_clusters") or 3)
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{'●' if current_clusters == n else '○'} {n}+",
                callback_data=f"clusters:{n}",
            )
            for n in (2, 3, 4, 5)
        ]
    )

    size = user_size(user)
    for group in (SIZE_OPTIONS[:3], SIZE_OPTIONS[3:]):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'●' if abs(size - n) < 0.01 else '○'} ${n}",
                    callback_data=f"size:{n}",
                )
                for n in group
            ]
        )

    ceiling = user.get("max_cost_pct")
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{'●' if _ceiling_active(ceiling, n) else '○'} "
                + ("kapalı" if n == 0 else f"≤%{n}"),
                callback_data=f"cost:{n}",
            )
            for n in COST_OPTIONS
        ]
    )

    caps = (("<100B", 100_000), ("<500B", 500_000), ("<2M", 2_000_000), ("hepsi", 0))
    active_cap = user.get("max_mcap_usd")
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{'●' if _same(active_cap, value) else '○'} {label}",
                callback_data=f"maxmc:{value}",
            )
            for label, value in caps
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "🛡 Riskli tokenleri ele: AÇIK"
                    if user.get("require_safe")
                    else "🛡 Riskli tokenleri ele: KAPALI"
                ),
                callback_data="safe:toggle",
            )
        ]
    )
    if (url := panel_url(user)) is not None:
        rows.append([InlineKeyboardButton(text="🔬 Bu ayarları Strateji Lab'da test et", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_url(user: dict[str, Any]) -> str | None:
    """A Strategy Lab link carrying this user's filter as query parameters.

    One-way on purpose: the panel is anonymous and prefilling a form needs no
    authentication, whereas letting a web page write back to someone's alert
    settings would. So the link hands the settings over for research; changing
    them stays in Telegram.
    """
    base = get_settings().public_base_url.rstrip("/")
    if not base or base.startswith("http://localhost"):
        return None

    params: dict[str, str] = {
        "size": f"{user_size(user):g}",
        "clusters": str(int(user.get("min_clusters") or 3)),
        "chains": ",".join(user.get("chains") or []),
    }
    if user.get("max_mcap_usd"):
        params["maxmc"] = f"{float(user['max_mcap_usd']):g}"
    if user.get("min_liquidity_usd"):
        params["minliq"] = f"{float(user['min_liquidity_usd']):g}"
    if user.get("require_safe"):
        params["safe"] = "1"
    return f"{base}/?{urlencode(params)}"


def _ceiling_active(current: Any, option: int) -> bool:
    if option == 0:
        return current is None
    return current is not None and abs(float(current) - option) < 0.01


def _same(current: Any, value: int) -> bool:
    if value == 0:
        return current is None
    return current is not None and abs(float(current) - value) < 1.0


def _filters_summary(user: dict[str, Any]) -> str:
    """Current values, listed in the same order as the button rows below.

    The buttons are too narrow to label themselves, so this is what tells the
    reader which row is which.
    """
    size = user_size(user)
    ceiling = user.get("max_cost_pct")
    chains = user.get("chains") or []
    cap = user.get("max_mcap_usd")

    chain_names = ", ".join(chain.short_name for key, chain in ALL_CHAINS.items() if key in chains)
    if cap:
        cap_text = f"{usd(float(cap))} altı"
    else:
        cap_text = "sınırsız"

    lines = [
        "<b>Ayarların</b>",
        "",
        f"🔗 Zincirler: <b>{chain_names or 'yok'}</b>",
        f"👥 Min bağımsız cüzdan: <b>{int(user.get('min_clusters') or 3)}</b>",
        f"💰 İşlem başına: <b>{usd(size)}</b>",
        (
            f"💸 Maliyet tavanı: <b>%{tr_num(float(ceiling), 0)}</b>"
            if ceiling
            else "💸 Maliyet tavanı: <b>kapalı</b>"
        ),
        f"📊 Market cap: <b>{cap_text}</b>",
        (
            "🛡 Riskli tokenler: <b>eleniyor</b>"
            if user.get("require_safe")
            else "🛡 Riskli tokenler: <b>gönderiliyor</b>"
        ),
    ]

    # Concrete consequence of the chosen size, per chain. This is the whole
    # point of the sizing control, so it belongs in front of the buttons.
    lines += ["", f"<i>{usd(size)} ile $40b likiditeli bir havuzda:</i>"]
    for chain in ALL_CHAINS.values():
        cost_pct, break_even = trade_economics(
            liquidity_usd=40_000.0, chain=chain, trade_size_usd=size
        )
        lines.append(
            f"  {chain.short_name}: %{tr_num(cost_pct, 2)} → başabaş {tr_num(break_even, 3)}x"
        )

    lines += [
        "",
        "<i>Düğme sırası: zincir · cüzdan · işlem boyutu · maliyet tavanı · "
        "market cap · güvenlik</i>",
    ]
    return "\n".join(lines)


def user_filter_spec(user: dict[str, Any]) -> FilterSpec:
    """Translate a user's saved filters into a backtest FilterSpec."""
    chain_ids = tuple(
        get_chain(key).chain_id for key in (user.get("chains") or []) if key in ALL_CHAINS
    )
    return FilterSpec(
        chains=chain_ids or None,
        min_clusters=int(user["min_clusters"]) if user.get("min_clusters") else None,
        min_mcap_usd=float(user["min_mcap_usd"]) if user.get("min_mcap_usd") else None,
        max_mcap_usd=float(user["max_mcap_usd"]) if user.get("max_mcap_usd") else None,
        min_liquidity_usd=(
            float(user["min_liquidity_usd"]) if user.get("min_liquidity_usd") else None
        ),
        max_age_minutes=int(user["max_age_minutes"]) if user.get("max_age_minutes") else None,
        safety_verdicts=("safe", "caution", "unknown") if user.get("require_safe") else None,
        min_quality=float(user["min_quality"]) if user.get("min_quality") else None,
    )


# --- handlers ----------------------------------------------------------


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    if message.from_user is None:
        return
    user = await repo.ensure_user(message.from_user.id, message.from_user.username)
    trial = user.get("trial_ends_at")
    suffix = ""
    if trial and trial > datetime.now(UTC):
        days = max(0, (trial - datetime.now(UTC)).days)
        suffix = f"\n\n🎁 Deneme süresi: <b>{days} gün</b> kaldı."
    await message.answer(WELCOME + suffix, reply_markup=_menu())


@router.message(Command("help", "yardim"))
async def on_help(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=_menu())


@router.message(Command("ayarlar", "filters"))
async def on_filters(message: Message) -> None:
    if message.from_user is None:
        return
    user = await repo.ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(_filters_summary(user), reply_markup=_filters_keyboard(user))


@router.message(Command("pause", "durdur"))
async def on_pause(message: Message) -> None:
    if message.from_user is None:
        return
    await repo.set_alerts_paused(message.from_user.id, True)
    await message.answer("Bildirimler durduruldu. Tekrar açmak için /resume yaz.")


@router.message(Command("resume", "devam"))
async def on_resume(message: Message) -> None:
    if message.from_user is None:
        return
    await repo.set_alerts_paused(message.from_user.id, False)
    await message.answer("Bildirimler tekrar açıldı.")


@router.message(Command("stats", "karne"))
async def on_stats(message: Message) -> None:
    if message.from_user is None:
        return
    notice = await message.answer("Hesaplıyorum…")
    await notice.edit_text(await build_stats(message.from_user.id, message.from_user.username))


@router.message(Command("top"))
async def on_top(message: Message) -> None:
    await message.answer(await build_top())


@router.message(Command("durum", "status", "diagnose"))
async def on_status(message: Message) -> None:
    """Full system diagnosis in plain language - the owner's alternative to logs."""
    notice = await message.answer("Kontrol ediyorum…")
    try:
        report = await diagnostics.run()
        await notice.edit_text(report.as_telegram())
    except Exception as exc:
        log.exception("diagnostics.failed", error=str(exc))
        await notice.edit_text(
            "Teşhis çalıştırılamadı. Bu genelde veritabanına ulaşılamadığı anlamına gelir.\n"
            f"<code>{type(exc).__name__}</code>"
        )


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(query: CallbackQuery) -> None:
    if query.from_user is None or not isinstance(query.data, str):
        return
    action = query.data.split(":", 1)[1]
    await query.answer()
    target = query.message
    if target is None:
        return

    if action == "filters":
        user = await repo.ensure_user(query.from_user.id, query.from_user.username)
        await target.answer(_filters_summary(user), reply_markup=_filters_keyboard(user))
    elif action == "stats":
        await target.answer(await build_stats(query.from_user.id, query.from_user.username))
    elif action == "top":
        await target.answer(await build_top())
    elif action == "status":
        report = await diagnostics.run()
        await target.answer(report.as_telegram())
    else:
        await target.answer(WELCOME, reply_markup=_menu())


@router.callback_query(F.data.regexp(r"^(chain|clusters|maxmc|safe|size|cost):"))
async def on_filter_change(query: CallbackQuery) -> None:
    if query.from_user is None or not isinstance(query.data, str):
        return
    kind, value = query.data.split(":", 1)
    user = await repo.ensure_user(query.from_user.id, query.from_user.username)
    toast = "Kaydedildi"

    if kind == "chain":
        chains = set(user.get("chains") or [])
        chains.symmetric_difference_update({value})
        if not chains:  # never leave a user with nothing selected
            chains = {value}
            toast = "En az bir zincir açık kalmalı"
        await repo.update_filter(query.from_user.id, "chains", sorted(chains))
    elif kind == "clusters":
        await repo.update_filter(query.from_user.id, "min_clusters", int(value))
    elif kind == "size":
        await repo.update_filter(query.from_user.id, "trade_size_usd", float(value))
        toast = f"İşlem boyutu ${value}"
    elif kind == "cost":
        limit = float(value)
        await repo.update_filter(query.from_user.id, "max_cost_pct", None if limit == 0 else limit)
        toast = "Maliyet tavanı kaldırıldı" if limit == 0 else f"Maliyet tavanı %{value}"
    elif kind == "maxmc":
        amount = int(value)
        await repo.update_filter(
            query.from_user.id, "max_mcap_usd", None if amount == 0 else amount
        )
    else:
        await repo.update_filter(
            query.from_user.id, "require_safe", not bool(user.get("require_safe"))
        )

    await query.answer(toast)
    updated = await repo.get_user(query.from_user.id) or {}
    # An InaccessibleMessage (too old to edit) has no edit_text; the isinstance
    # check is what keeps the type honest rather than a suppressed exception.
    if isinstance(query.message, Message):
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _filters_summary(updated), reply_markup=_filters_keyboard(updated)
            )


# --- content builders --------------------------------------------------


async def build_stats(telegram_id: int, username: str | None) -> str:
    user = await repo.ensure_user(telegram_id, username)
    spec = user_filter_spec(user)
    size = user_size(user)
    signals = await repo.signals_for_backtest(STATS_WINDOW_DAYS)
    result = run(
        signals,
        spec=spec,
        exit_model=by_name(STATS_EXIT_MODEL),
        size_usd=size,
        cost=CostModel(),
    )
    return format_stats(
        result,
        filter_label=spec.label(),
        window_days=STATS_WINDOW_DAYS,
        trade_size_usd=size,
    )


async def build_top(limit: int = 10) -> str:
    lines = [
        "<b>En yüksek puanlı cüzdanlar</b>",
        "<i>Puan; yakın dönem kârı, bir önsele çekilmiş kazanç oranı,</i>",
        "<i>medyan çarpan ve farklı token sayısını birleştirir.</i>",
    ]
    found = False
    for chain in ALL_CHAINS.values():
        wallets = await repo.top_wallets(chain.chain_id, limit)
        if not wallets:
            continue
        found = True
        lines += ["", f"<b>{chain.name}</b>"]
        for rank, wallet in enumerate(wallets, 1):
            parts = [f"puan {float(wallet['score']):.0f}"]
            if wallet.get("win_rate") is not None:
                parts.append(f"kazanç %{float(wallet['win_rate']) * 100:.0f}")
            if wallet.get("median_multiple") is not None:
                parts.append(f"medyan {tr_num(float(wallet['median_multiple']))}x")
            parts.append(f"{wallet['closed_trades']} işlem")
            lines.append(f"{rank}. <code>{wallet['wallet'][:10]}…</code> " + " · ".join(parts))
    if not found:
        lines.append("\nHenüz puanlanmış cüzdan yok — keşif ve puanlama sürüyor.")
    return "\n".join(lines)


# --- alert dispatch ----------------------------------------------------


def _recipient_size(recipient: dict[str, Any]) -> float:
    raw = recipient.get("trade_size_usd")
    try:
        return float(raw) if raw else DEFAULT_TRADE_SIZE_USD
    except (TypeError, ValueError):
        return DEFAULT_TRADE_SIZE_USD


async def dispatch_alerts(bot: Bot, *, limit: int = 25) -> int:
    """Send pending signals to every user whose filters they match.

    Each message is rendered per recipient, because the round-trip cost - the
    line that decides whether the trade is worth taking - depends on their
    position size.
    """
    pending = await repo.pending_alert_signals(limit)
    sent = 0
    for signal in pending:
        chain = BY_CHAIN_ID.get(int(signal["chain_id"]))
        if chain is None:
            await repo.mark_alert_sent(int(signal["id"]))
            continue

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, url=url)]
                for label, url in signal_links(signal, chain)[:3]
            ]
        )
        liquidity = signal.get("snap_liquidity_usd")
        liquidity_usd = float(liquidity) if liquidity is not None else None

        for recipient in await repo.recipients_for(signal, chain.key):
            telegram_id = int(recipient["telegram_id"])
            size = _recipient_size(recipient)

            ceiling = recipient.get("max_cost_pct")
            if ceiling is not None:
                cost_pct, _ = trade_economics(
                    liquidity_usd=liquidity_usd, chain=chain, trade_size_usd=size
                )
                if cost_pct > float(ceiling):
                    # Uneconomic at this user's size: mark it seen so it is not
                    # reconsidered, but send nothing.
                    await repo.record_alert(telegram_id, int(signal["id"]))
                    continue

            try:
                await bot.send_message(
                    telegram_id,
                    format_signal(signal, chain, trade_size_usd=size),
                    reply_markup=keyboard,
                )
                await repo.record_alert(telegram_id, int(signal["id"]))
                sent += 1
            except TelegramForbiddenError:
                # The user blocked the bot: stop trying forever.
                await repo.block_user(telegram_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except Exception as exc:
                log.warning("alert.send_failed", user=telegram_id, error=str(exc))
            # Telegram allows ~30 messages/second; stay well under it.
            await asyncio.sleep(0.05)

        await repo.mark_alert_sent(int(signal["id"]))
    if sent:
        log.info("alerts.dispatched", messages=sent, signals=len(pending))
    return sent


async def alert_loop(bot: Bot) -> None:
    while True:
        try:
            await dispatch_alerts(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("alerts.loop_error", error=str(exc))
        await asyncio.sleep(ALERT_POLL_SECONDS)


async def main() -> None:
    setup_logging()
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    await db.init_pool(max_size=5)
    await db.migrate()

    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    alerts = asyncio.create_task(alert_loop(bot), name="alerts")
    log.info("bot.started")
    try:
        await dispatcher.start_polling(bot, handle_signals=False)
    finally:
        alerts.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await alerts
        await bot.session.close()
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
