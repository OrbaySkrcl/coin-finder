"""Worker entrypoint: runs every ingestion and scoring loop."""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from coinfinder import db
from coinfinder.chains import enabled_chains
from coinfinder.config import get_settings
from coinfinder.ingest.prices import NativePriceOracle
from coinfinder.logging_setup import setup_logging
from coinfinder.rpc.pool import RpcPool
from coinfinder.signals import quality
from coinfinder.sources.dexscreener import DexScreenerClient
from coinfinder.worker import pipeline

log = structlog.get_logger(__name__)

# Cadences chosen by cost: watching wallets is cheap, rescoring is not.
RESCORE_INTERVAL = 6 * 3600
DISCOVERY_INTERVAL = 3600
OUTCOME_INTERVAL = 300
SIGNAL_INTERVAL = 60
REFIT_INTERVAL = 12 * 3600


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.chains = enabled_chains(self.settings.chain_keys)
        self.client = DexScreenerClient()
        self.oracle = NativePriceOracle(self.client)
        self.model: quality.QualityModel = quality.PRIOR
        self.rpc: dict[str, RpcPool] = {}

    def _rpc_for(self, chain_key: str) -> RpcPool:
        if chain_key not in self.rpc:
            from coinfinder.chains import get_chain

            chain = get_chain(chain_key)
            urls = self.settings.rpc_override(chain_key) or list(chain.rpc_urls)
            # Public endpoints are shared infrastructure: stay well under any
            # plausible per-IP limit rather than probing for it.
            self.rpc[chain_key] = RpcPool(urls, requests_per_second=3.0)
        return self.rpc[chain_key]

    async def start(self) -> None:
        await db.init_pool(max_size=8)
        await db.migrate()
        self.model = await pipeline.refit_quality_model()

        tasks: list[asyncio.Task] = []
        for chain in self.chains:
            rpc = self._rpc_for(chain.key)
            tasks += [
                asyncio.create_task(
                    pipeline.run_forever(
                        f"watch:{chain.key}",
                        self.settings.poll_interval_seconds,
                        lambda c=chain, r=rpc: pipeline.watch_wallets(  # type: ignore[misc]
                            r, c, self.settings, self.oracle
                        ),
                    ),
                    name=f"watch:{chain.key}",
                ),
                asyncio.create_task(
                    pipeline.run_forever(
                        f"signals:{chain.key}",
                        SIGNAL_INTERVAL,
                        lambda c=chain, r=rpc: pipeline.emit_signals(  # type: ignore[misc]
                            r, c, self.settings, self.client, self.model
                        ),
                    ),
                    name=f"signals:{chain.key}",
                ),
                asyncio.create_task(
                    pipeline.run_forever(
                        f"rescore:{chain.key}",
                        RESCORE_INTERVAL,
                        lambda c=chain: pipeline.rescore(c, self.settings),  # type: ignore[misc]
                    ),
                    name=f"rescore:{chain.key}",
                ),
                asyncio.create_task(
                    pipeline.run_forever(
                        f"discover:{chain.key}",
                        DISCOVERY_INTERVAL,
                        lambda c=chain, r=rpc: pipeline.discover(  # type: ignore[misc]
                            r, c, self.settings, self.client
                        ),
                    ),
                    name=f"discover:{chain.key}",
                ),
                asyncio.create_task(
                    pipeline.run_forever(
                        f"outcomes:{chain.key}",
                        OUTCOME_INTERVAL,
                        lambda c=chain: pipeline.track_outcomes(c, self.client),  # type: ignore[misc]
                    ),
                    name=f"outcomes:{chain.key}",
                ),
            ]

        tasks.append(
            asyncio.create_task(
                pipeline.run_forever("refit", REFIT_INTERVAL, self._refit), name="refit"
            )
        )

        log.info("worker.started", chains=[c.key for c in self.chains], loops=len(tasks))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await self.close()

    async def _refit(self) -> None:
        self.model = await pipeline.refit_quality_model()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()
        for rpc in self.rpc.values():
            with contextlib.suppress(Exception):
                await rpc.close()
        await db.close_pool()


async def main() -> None:
    setup_logging()
    worker = Worker()
    try:
        await worker.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("worker.stopping")
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
