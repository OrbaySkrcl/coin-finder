"""Telegram bot: alerts, per-user filters, and honest performance reporting."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

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

from coinfinder import db, repo
from coinfinder.backtest.costs import CostModel
from coinfinder.backtest.engine import FilterSpec, run
from coinfinder.backtest.exits import by_name
from coinfinder.bot.format import format_signal, format_stats, signal_links
from coinfinder.chains import ALL_CHAINS, BY_CHAIN_ID, get_chain
from coinfinder.config import get_settings
from coinfinder.logging_setup import setup_logging

log = structlog.get_logger(__name__)
router = Router()

ALERT_POLL_SECONDS = 15
STATS_WINDOW_DAYS = 30
STATS_EXIT_MODEL = "ladder"

WELCOME = """<b>coin-finder</b> — smart-money signals

I watch wallets with a proven track record on Base, Robinhood Chain and BNB
Chain, and alert you when several <i>independent</i> ones buy the same token.

What is different here:
• Conviction counts independent operators, not addresses. Five wallets funded
  from one source count once.
• Quality is a probability you can check, not a star rating.
• Every alert shows what the round trip actually costs at your position size,
  and the multiple you need just to break even.
• /stats replays <i>your</i> filter over real history, net of fees, slippage
  and gas, with a confidence interval.

These are data, not advice. Most tokens in this market go to zero.

/filters — tune what reaches you
/stats — what your filter actually returned
/top — highest-scoring wallets
/pause — stop alerts"""


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚙️ Filters", callback_data="menu:filters"),
                InlineKeyboardButton(text="📊 My stats", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton(text="🏆 Top wallets", callback_data="menu:top"),
                InlineKeyboardButton(text="❓ Help", callback_data="menu:help"),
            ],
        ]
    )


def _filters_keyboard(user: dict[str, Any]) -> InlineKeyboardMarkup:
    chains = set(user.get("chains") or [])
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if key in chains else '⬜'} {chain.name}",
                callback_data=f"chain:{key}",
            )
        ]
        for key, chain in ALL_CHAINS.items()
    ]
    current = int(user.get("min_clusters") or 3)
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{'●' if current == n else '○'} {n}+ wallets", callback_data=f"clusters:{n}"
            )
            for n in (2, 3, 4, 5)
        ]
    )
    caps = [("<100k", 100_000), ("<500k", 500_000), ("<2M", 2_000_000), ("any", 0)]
    active = user.get("max_mcap_usd")
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{'●' if _same(active, value) else '○'} MC {label}",
                callback_data=f"maxmc:{value}",
            )
            for label, value in caps
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{'🛡 Block risky: ON' if user.get('require_safe') else '🛡 Block risky: OFF'}",
                callback_data="safe:toggle",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _same(current: Any, value: int) -> bool:
    if value == 0:
        return current is None
    return current is not None and abs(float(current) - value) < 1.0


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
        suffix = f"\n\n🎁 Free trial: <b>{days} day(s)</b> remaining."
    await message.answer(WELCOME + suffix, reply_markup=_menu())


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=_menu())


@router.message(Command("filters"))
async def on_filters(message: Message) -> None:
    if message.from_user is None:
        return
    user = await repo.ensure_user(message.from_user.id, message.from_user.username)
    await message.answer("<b>Your filters</b>", reply_markup=_filters_keyboard(user))


@router.message(Command("pause"))
async def on_pause(message: Message) -> None:
    if message.from_user is None:
        return
    await repo.set_alerts_paused(message.from_user.id, True)
    await message.answer("Alerts paused. /resume to turn them back on.")


@router.message(Command("resume"))
async def on_resume(message: Message) -> None:
    if message.from_user is None:
        return
    await repo.set_alerts_paused(message.from_user.id, False)
    await message.answer("Alerts resumed.")


@router.message(Command("stats"))
async def on_stats(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await build_stats(message.from_user.id, message.from_user.username))


@router.message(Command("top"))
async def on_top(message: Message) -> None:
    await message.answer(await build_top())


@router.message(Command("status"))
async def on_status(message: Message) -> None:
    stats = await repo.system_stats()
    lines = ["<b>System</b>"]
    for key, value in stats.items():
        lines.append(f"{key.replace('_', ' ')}: <b>{value}</b>")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(query: CallbackQuery) -> None:
    if query.from_user is None or not isinstance(query.data, str):
        return
    action = query.data.split(":", 1)[1]
    await query.answer()
    if action == "filters":
        user = await repo.ensure_user(query.from_user.id, query.from_user.username)
        await query.message.answer(  # type: ignore[union-attr]
            "<b>Your filters</b>", reply_markup=_filters_keyboard(user)
        )
    elif action == "stats":
        text = await build_stats(query.from_user.id, query.from_user.username)
        await query.message.answer(text)  # type: ignore[union-attr]
    elif action == "top":
        await query.message.answer(await build_top())  # type: ignore[union-attr]
    else:
        await query.message.answer(WELCOME, reply_markup=_menu())  # type: ignore[union-attr]


@router.callback_query(F.data.regexp(r"^(chain|clusters|maxmc|safe):"))
async def on_filter_change(query: CallbackQuery) -> None:
    if query.from_user is None or not isinstance(query.data, str):
        return
    kind, value = query.data.split(":", 1)
    user = await repo.ensure_user(query.from_user.id, query.from_user.username)

    if kind == "chain":
        chains = set(user.get("chains") or [])
        chains.symmetric_difference_update({value})
        if not chains:  # never leave a user with nothing selected
            chains = {value}
        await repo.update_filter(query.from_user.id, "chains", sorted(chains))
    elif kind == "clusters":
        await repo.update_filter(query.from_user.id, "min_clusters", int(value))
    elif kind == "maxmc":
        amount = int(value)
        await repo.update_filter(
            query.from_user.id, "max_mcap_usd", None if amount == 0 else amount
        )
    else:
        await repo.update_filter(
            query.from_user.id, "require_safe", not bool(user.get("require_safe"))
        )

    await query.answer("Saved")
    updated = await repo.get_user(query.from_user.id) or {}
    with contextlib.suppress(Exception):
        await query.message.edit_reply_markup(  # type: ignore[union-attr]
            reply_markup=_filters_keyboard(updated)
        )


# --- content builders --------------------------------------------------


async def build_stats(telegram_id: int, username: str | None) -> str:
    user = await repo.ensure_user(telegram_id, username)
    spec = user_filter_spec(user)
    signals = await repo.signals_for_backtest(STATS_WINDOW_DAYS)
    result = run(
        signals,
        spec=spec,
        exit_model=by_name(STATS_EXIT_MODEL),
        size_usd=100.0,
        cost=CostModel(),
    )
    return format_stats(result, filter_label=spec.label(), window_days=STATS_WINDOW_DAYS)


async def build_top(limit: int = 10) -> str:
    lines = [
        "<b>Top-scoring wallets</b>",
        "<i>Score blends recent PnL, win rate shrunk toward a prior,</i>",
        "<i>median multiple, and breadth across distinct tokens.</i>",
    ]
    found = False
    for chain in ALL_CHAINS.values():
        wallets = await repo.top_wallets(chain.chain_id, limit)
        if not wallets:
            continue
        found = True
        lines += ["", f"<b>{chain.name}</b>"]
        for rank, wallet in enumerate(wallets, 1):
            parts = [f"score {float(wallet['score']):.0f}"]
            if wallet.get("win_rate") is not None:
                parts.append(f"win {float(wallet['win_rate']):.0%}")
            if wallet.get("median_multiple") is not None:
                parts.append(f"med {float(wallet['median_multiple']):.2f}x")
            parts.append(f"{wallet['closed_trades']} trades")
            lines.append(f"{rank}. <code>{wallet['wallet'][:10]}…</code> " + " · ".join(parts))
    if not found:
        lines.append("\nNo wallets scored yet — discovery and scoring are still warming up.")
    return "\n".join(lines)


# --- alert dispatch ----------------------------------------------------


async def dispatch_alerts(bot: Bot, *, limit: int = 25) -> int:
    """Send pending signals to every user whose filters they match."""
    pending = await repo.pending_alert_signals(limit)
    sent = 0
    for signal in pending:
        chain = BY_CHAIN_ID.get(int(signal["chain_id"]))
        if chain is None:
            await repo.mark_alert_sent(int(signal["id"]))
            continue

        text = format_signal(signal, chain)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, url=url)]
                for label, url in signal_links(signal, chain)[:3]
            ]
        )
        for telegram_id in await repo.recipients_for(signal, chain.key):
            try:
                await bot.send_message(telegram_id, text, reply_markup=keyboard)
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
