"""All-in-one entrypoint: API, worker and bot in a single process.

Running the three parts as separate services is the better shape for scale,
but it triples the setup: three deployments, three start commands, three
places for a mistake to hide. For someone who is not going to read logs, one
service that either works or does not is worth far more than a tidy topology.

Everything here is asyncio, so all three share one event loop with no extra
machinery. Set ``RUN_COMPONENTS`` to split them out later - the code does not
change, only which parts each process starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import structlog
import uvicorn

from coinfinder import db
from coinfinder.config import get_settings
from coinfinder.logging_setup import setup_logging

log = structlog.get_logger(__name__)

ALL_COMPONENTS = ("api", "worker", "bot")


def selected_components() -> list[str]:
    raw = os.environ.get("RUN_COMPONENTS", "").strip()
    if not raw:
        return list(ALL_COMPONENTS)
    chosen = [c.strip().lower() for c in raw.split(",") if c.strip()]
    unknown = [c for c in chosen if c not in ALL_COMPONENTS]
    if unknown:
        raise SystemExit(
            f"RUN_COMPONENTS contains unknown component(s): {', '.join(unknown)}. "
            f"Valid values: {', '.join(ALL_COMPONENTS)}"
        )
    return chosen


async def run_api() -> None:
    from coinfinder.api.main import app

    # Railway injects PORT; fall back to the configured API port locally.
    port = int(os.environ.get("PORT") or get_settings().api_port)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_config=None,
        access_log=False,
    )
    await uvicorn.Server(config).serve()


async def run_worker() -> None:
    from coinfinder.worker.main import Worker

    worker = Worker()
    try:
        await worker.start()
    finally:
        await worker.close()


async def run_bot() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        # Not fatal: the dashboard and ingestion are useful on their own, and
        # a missing token is a setup step the user has not reached yet.
        #
        # Park rather than return. The supervisor treats any component
        # finishing as a reason to restart the process, so returning here
        # turned "no token yet" into an invisible restart loop - the single
        # worst outcome for an operator who does not read logs.
        log.warning("bot.disabled", reason="TELEGRAM_BOT_TOKEN is not set")
        await asyncio.Event().wait()
        return

    import contextlib as _contextlib

    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from coinfinder.bot.main import alert_loop, configure_commands, router

    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await configure_commands(bot)
    alerts = asyncio.create_task(alert_loop(bot), name="alerts")
    try:
        await dispatcher.start_polling(bot, handle_signals=False)
    finally:
        alerts.cancel()
        with _contextlib.suppress(asyncio.CancelledError):
            await alerts
        await bot.session.close()


COMPONENT_RUNNERS = {"api": run_api, "worker": run_worker, "bot": run_bot}

#: A managed database can take a few seconds to accept connections after the
#: app container starts, so a first failure is expected rather than fatal.
DB_CONNECT_ATTEMPTS = 6


async def connect_with_retry() -> bool:
    """Open the pool and migrate, retrying with backoff. False if unreachable."""
    delay = 2.0
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        try:
            await db.init_pool(max_size=10)
            applied = await db.migrate()
            if applied:
                log.info("startup.migrated", migrations=applied)
            return True
        except Exception as exc:
            log.warning(
                "startup.db_unavailable",
                attempt=attempt,
                of=DB_CONNECT_ATTEMPTS,
                error=str(exc)[:200],
            )
            with contextlib.suppress(Exception):
                await db.close_pool()
            if attempt == DB_CONNECT_ATTEMPTS:
                return False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    return False


async def main() -> None:
    setup_logging()
    components = selected_components()

    # Migrations run once here rather than in the start command, so the
    # deployment is a single unconditional "start the app".
    database_ready = await connect_with_retry()

    if not database_ready:
        if "api" not in components:
            raise SystemExit(
                "Database unreachable and no API component to report it. "
                "Check that a PostgreSQL plugin is attached and DATABASE_URL is set."
            )
        # Serve the API anyway so the operator sees a diagnosis page instead of
        # a silent restart loop. A crash loop tells a non-technical operator
        # nothing at all; a page saying "database unreachable" tells them
        # exactly which setting to fix.
        log.error("startup.degraded", reason="database unreachable, serving diagnostics only")
        components = ["api"]

    tasks = [asyncio.create_task(COMPONENT_RUNNERS[name](), name=name) for name in components]
    log.info("startup.ready", components=components)

    try:
        # Every runner is expected to run forever, so any of them finishing -
        # cleanly or not - means something is wrong and the process should be
        # restarted. A half-running system is the hardest kind to notice.
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            if (error := task.exception()) is not None:
                log.error("component.crashed", component=task.get_name(), error=str(error))
                raise error
            log.warning("component.exited", component=task.get_name())
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await db.close_pool()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutdown.interrupted")


if __name__ == "__main__":
    run()
