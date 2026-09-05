"""Plain-language system diagnostics.

The owner of this system does not read logs. So every check here answers the
only three questions that matter to them:

    Is it broken?  What is it doing right now?  When will I see signals?

Owner-facing text is Turkish because that is who reads it. Machine-readable
``code`` fields stay English so the dashboard and tests can branch on them.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog

from coinfinder import db
from coinfinder.chains import Chain, enabled_chains
from coinfinder.config import Settings, get_settings
from coinfinder.rpc.pool import RpcPool
from coinfinder.sources.dexscreener import DexScreenerClient, best_pair

log = structlog.get_logger(__name__)


class Level(StrEnum):
    OK = "ok"
    WAITING = "waiting"
    WARN = "warn"
    ERROR = "error"


ICON = {Level.OK: "✅", Level.WAITING: "⏳", Level.WARN: "⚠️", Level.ERROR: "❌"}


@dataclass(slots=True)
class Check:
    code: str
    level: Level
    title: str
    detail: str

    def as_line(self) -> str:
        return f"{ICON[self.level]} <b>{self.title}</b>\n     {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "level": str(self.level), "icon": ICON[self.level]}


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)
    headline: str = ""
    headline_level: Level = Level.OK
    progress: list[dict[str, Any]] = field(default_factory=list)

    @property
    def worst(self) -> Level:
        for level in (Level.ERROR, Level.WARN, Level.WAITING):
            if any(c.level is level for c in self.checks):
                return level
        return Level.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "headline_level": str(self.headline_level),
            "headline_icon": ICON[self.headline_level],
            "worst_level": str(self.worst),
            "checks": [c.to_dict() for c in self.checks],
            "progress": self.progress,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def as_telegram(self) -> str:
        lines = [f"{ICON[self.headline_level]} <b>{self.headline}</b>", ""]
        lines += [c.as_line() for c in self.checks]
        if self.progress:
            lines += ["", "<b>Kurulum ilerlemesi</b>"]
            for step in self.progress:
                mark = "✅" if step["done"] else ("⏳" if step["active"] else "▫️")
                lines.append(f"{mark} {step['label']} — {step['detail']}")
        return "\n".join(lines)


# --- individual checks --------------------------------------------------


async def check_database() -> Check:
    try:
        await db.fetchval("SELECT 1")
    except Exception as exc:
        return Check(
            code="database",
            level=Level.ERROR,
            title="Veritabanı",
            detail=(
                "Bağlanamıyorum. Railway'de PostgreSQL eklentisinin ekli ve "
                f"DATABASE_URL değişkeninin tanımlı olduğunu kontrol et. ({type(exc).__name__})"
            ),
        )
    return Check("database", Level.OK, "Veritabanı", "Bağlı ve yazılabilir.")


async def check_chain(chain: Chain, settings: Settings) -> Check:
    urls = settings.rpc_override(chain.key) or list(chain.rpc_urls)
    pool = RpcPool(urls, requests_per_second=3.0, max_attempts=2, timeout_seconds=10.0)
    try:
        async with pool:
            block = await pool.block_number()
    except Exception:
        return Check(
            code=f"rpc:{chain.key}",
            level=Level.ERROR,
            title=f"{chain.name} bağlantısı",
            detail=(
                f"{len(urls)} adresin hiçbiri cevap vermedi. Bu zincirden veri gelmeyecek. "
                "Genelde geçicidir; birkaç saat sürerse haber ver."
            ),
        )
    return Check(
        code=f"rpc:{chain.key}",
        level=Level.OK,
        title=f"{chain.name} bağlantısı",
        detail=f"Bağlı, blok {block:,}.",
    )


async def check_market_data(chains: list[Chain]) -> Check:
    async with DexScreenerClient() as client:
        for chain in chains:
            if not int(chain.wrapped_native, 16):
                continue
            try:
                pairs = await client.pairs_for_tokens(
                    chain.dexscreener_slug, [chain.wrapped_native]
                )
            except Exception:
                continue
            pair = best_pair(pairs, chain.wrapped_native)
            if pair and pair.price_usd:
                return Check(
                    code="market_data",
                    level=Level.OK,
                    title="Fiyat verisi (DexScreener)",
                    detail=f"Bağlı. Örnek: {pair.base_symbol} ${pair.price_usd:,.2f}.",
                )
    return Check(
        code="market_data",
        level=Level.ERROR,
        title="Fiyat verisi (DexScreener)",
        detail=(
            "Ulaşamıyorum. Fiyat, market cap ve likidite bilgisi buradan geliyor; "
            "bu olmadan sinyal üretilemez."
        ),
    )


async def check_telegram(settings: Settings) -> Check:
    if not settings.telegram_bot_token:
        return Check(
            code="telegram",
            level=Level.WARN,
            title="Telegram botu",
            detail=(
                "Token tanımlı değil, bot kapalı. @BotFather'dan token alıp "
                "TELEGRAM_BOT_TOKEN değişkenine yaz."
            ),
        )
    return Check("telegram", Level.OK, "Telegram botu", "Token tanımlı, bot açık.")


# --- warm-up progress ---------------------------------------------------


async def counts() -> dict[str, Any]:
    row = await db.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM wallets) AS candidate_wallets,
          (SELECT count(*) FROM wallets WHERE watch_since IS NOT NULL AND NOT is_excluded)
              AS watched_wallets,
          (SELECT count(*) FROM wallet_trades) AS trades,
          (SELECT count(*) FROM wallet_scores WHERE is_smart) AS smart_wallets,
          (SELECT count(*) FROM signals) AS signals,
          (SELECT count(*) FROM signals WHERE ts > now() - interval '24 hours') AS signals_24h,
          (SELECT max(ts) FROM wallet_trades) AS last_trade_at,
          (SELECT max(ts) FROM signals) AS last_signal_at,
          (SELECT min(first_seen_at) FROM wallets) AS first_wallet_at
        """
    )
    return dict(row) if row else {}


def build_progress(c: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """Four setup milestones, in the order they actually happen."""
    candidates = int(c.get("candidate_wallets") or 0)
    trades = int(c.get("trades") or 0)
    smart = int(c.get("smart_wallets") or 0)
    signals = int(c.get("signals") or 0)

    steps = [
        {
            "key": "discovery",
            "label": "1. Aday cüzdanları bulma",
            "done": candidates > 0,
            "detail": (
                f"{candidates:,} cüzdan bulundu."
                if candidates
                else "Henüz başlamadı. İlk tarama ~1 saat içinde."
            ),
        },
        {
            "key": "indexing",
            "label": "2. İşlemlerini izleme",
            "done": trades > 0,
            "detail": (
                f"{trades:,} işlem kaydedildi." if trades else "Cüzdan bulunduktan sonra başlar."
            ),
        },
        {
            "key": "scoring",
            "label": "3. Cüzdanları puanlama",
            "done": smart > 0,
            "detail": (
                f"{smart:,} cüzdan 'akıllı para' olarak seçildi."
                if smart
                else (
                    f"Her cüzdan için en az {settings.smart_wallet_min_trades} tamamlanmış "
                    "alım-satım gerekiyor. 6 saatte bir çalışır."
                )
            ),
        },
        {
            "key": "signals",
            "label": "4. Sinyal üretme",
            "done": signals > 0,
            "detail": (
                f"{signals:,} sinyal üretildi, son 24 saatte {c.get('signals_24h') or 0}."
                if signals
                else (
                    f"Aynı tokeni {settings.confluence_min_clusters} bağımsız akıllı cüzdan "
                    "alınca tetiklenir."
                )
            ),
        },
    ]
    # The active step is the first unfinished one - but only while nothing
    # later has finished. Saying "starts after wallets are found" under a step
    # whose successors are already complete reads as a fault when it is not.
    last_done = max((i for i, s in enumerate(steps) if s["done"]), default=-1)
    active_marked = False
    for index, step in enumerate(steps):
        step["active"] = not step["done"] and not active_marked and index > last_done
        if step["active"]:
            active_marked = True
        elif not step["done"] and index < last_done:
            step["detail"] = "Bu adımda kayıt yok."
    return steps


def headline_for(
    progress: list[dict[str, Any]], c: dict[str, Any], worst: Level
) -> tuple[str, Level]:
    """One sentence answering 'is it working and when do I see signals?'."""
    if worst is Level.ERROR:
        return "Bir sorun var — aşağıdaki kırmızı satıra bak.", Level.ERROR

    if int(c.get("signals") or 0) > 0:
        last = c.get("last_signal_at")
        if last and datetime.now(UTC) - last > timedelta(hours=48):
            return (
                "Çalışıyor ama 2 gündür yeni sinyal yok. Filtreler fazla dar olabilir.",
                Level.WARN,
            )
        return "Her şey çalışıyor. Sistem sinyal üretiyor.", Level.OK

    first_seen = c.get("first_wallet_at")
    age_hours = (datetime.now(UTC) - first_seen).total_seconds() / 3600 if first_seen else 0.0
    current = next((s for s in progress if s["active"]), None)
    step_name = current["label"].split(". ", 1)[-1].lower() if current else "hazırlanıyor"

    if age_hours < 1:
        return f"Yeni kuruldu, {step_name} aşamasında. Bu normal.", Level.WAITING
    if age_hours < 72:
        return (
            f"Isınma sürüyor ({step_name}). İlk sinyaller için 2-4 gün normaldir.",
            Level.WAITING,
        )
    return (
        f"3 günden uzun süredir {step_name} aşamasında. Beklenenden yavaş — "
        "aşağıdaki adımlardan hangisinin takıldığına bak.",
        Level.WARN,
    )


# --- top level ----------------------------------------------------------


async def run(*, include_network: bool = True) -> Report:
    """Run every check. Network checks can be skipped for a fast page load."""
    settings = get_settings()
    chains = enabled_chains(settings.chain_keys)
    report = Report()

    report.checks.append(await check_database())
    if report.checks[0].level is Level.ERROR:
        # Nothing else can be established without the database.
        report.headline = "Veritabanına bağlanamıyorum. Önce onu düzeltmek gerek."
        report.headline_level = Level.ERROR
        return report

    if include_network:
        chain_checks = await asyncio.gather(
            *(check_chain(chain, settings) for chain in chains),
            check_market_data(chains),
            return_exceptions=True,
        )
        for result in chain_checks:
            if isinstance(result, Check):
                report.checks.append(result)
            else:
                log.warning("diagnostics.check_failed", error=str(result))

    report.checks.append(await check_telegram(settings))

    c = await counts()
    report.progress = build_progress(c, settings)
    report.headline, report.headline_level = headline_for(report.progress, c, report.worst)
    return report
