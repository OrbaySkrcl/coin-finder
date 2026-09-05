"""Background pipeline.

Each stage is an independent loop with its own cadence, because they have very
different costs. Watching wallets is cheap and must be fast; re-scoring every
wallet is expensive and only needs to happen a few times a day.

Every loop catches and logs its own exceptions. One failing chain or one
provider outage must never take the process down - on free infrastructure,
transient failure is the normal case, not the exception.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog

from coinfinder import repo
from coinfinder.chains import Chain
from coinfinder.config import Settings
from coinfinder.ingest import discovery, outcomes
from coinfinder.ingest.prices import NativePriceOracle
from coinfinder.ingest.wallet_watch import blocks_for_minutes, next_window, scan_range
from coinfinder.rpc.pool import RpcPool
from coinfinder.scoring import clustering
from coinfinder.scoring.pnl import compute_round_trips
from coinfinder.scoring.wallet_score import group_round_trips, rank_wallets
from coinfinder.signals import quality, safety
from coinfinder.signals.engine import (
    build_signal_payload,
    detect_confluence,
    passes_entry_filters,
)
from coinfinder.sources.dexscreener import DexScreenerClient, best_pair

log = structlog.get_logger(__name__)

WATCH_JOB = "wallet_watch"


async def run_forever(
    name: str, interval: float, fn: Callable[[], Awaitable[object]], *, jitter: float = 0.1
) -> None:
    """Run ``fn`` on a fixed cadence, surviving any error it raises."""
    import random

    while True:
        started = asyncio.get_event_loop().time()
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("pipeline.loop_error", loop=name, error=str(exc))
        elapsed = asyncio.get_event_loop().time() - started
        delay = max(1.0, interval - elapsed)
        await asyncio.sleep(delay * (1.0 + random.uniform(-jitter, jitter)))


# --- stage 1: watch tracked wallets -------------------------------------


async def watch_wallets(
    rpc: RpcPool, chain: Chain, settings: Settings, oracle: NativePriceOracle
) -> int:
    wallets = await repo.watched_wallets(chain.chain_id)
    if not wallets:
        log.info("pipeline.no_wallets", chain=chain.key)
        return 0

    head = await rpc.block_number()
    last_done = await repo.get_checkpoint(chain.chain_id, WATCH_JOB)
    if last_done == 0:
        # Cold start: begin one hour back rather than at genesis.
        last_done = max(0, head - blocks_for_minutes(chain, 60))

    window = next_window(
        last_done=last_done,
        head=head,
        confirmations=settings.reorg_confirmations,
        max_span=settings.log_range_blocks,
    )
    if window is None:
        return 0

    from_block, to_block = window
    trades = await scan_range(
        rpc,
        chain,
        wallets=wallets,
        from_block=from_block,
        to_block=to_block,
        batch_size=settings.wallet_watch_batch,
    )
    native_price = await oracle.price_usd(chain)
    written = await repo.insert_trades(chain.chain_id, trades, native_price)
    await repo.set_checkpoint(chain.chain_id, WATCH_JOB, to_block)
    return written


# --- stage 2: detect confluence and emit signals ------------------------


async def emit_signals(
    rpc: RpcPool,
    chain: Chain,
    settings: Settings,
    client: DexScreenerClient,
    model: quality.QualityModel,
) -> int:
    now = datetime.now(UTC)
    buys = await repo.recent_buys(
        chain.chain_id, now - timedelta(minutes=settings.confluence_window_minutes)
    )
    if not buys:
        return 0

    cluster_map = await repo.cluster_map(chain.chain_id)
    clusters = clustering.ClusterResult(mapping=cluster_map, multi_wallet_clusters={})
    candidates = detect_confluence(
        buys,
        clusters=clusters,
        now=now,
        window_minutes=settings.confluence_window_minutes,
        min_clusters=settings.confluence_min_clusters,
    )
    if not candidates:
        return 0

    # One DexScreener call covers up to 30 tokens.
    tokens = [c.token for c in candidates][:30]
    pairs = await client.pairs_for_tokens(chain.dexscreener_slug, tokens)

    emitted = 0
    for confluence in candidates[:30]:
        pair = best_pair(pairs, confluence.token)
        checks = await safety.onchain_checks(
            rpc,
            chain,
            token=confluence.token,
            pair_address=pair.pair_address if pair else None,
        )
        report = safety.assess(
            chain=chain,
            sells_observed=pair.sells_24h if pair else None,
            buys_observed=pair.buys_24h if pair else None,
            liquidity_usd=pair.liquidity_usd if pair else None,
            mcap_usd=pair.mcap_usd if pair else None,
            lp_burned_pct=checks.lp_burned_pct,
            owner_renounced=checks.owner_renounced,
            min_liquidity_usd=settings.min_liquidity_usd,
            age_minutes=pair.age_minutes if pair else None,
        )
        payload = build_signal_payload(
            chain=chain,
            confluence=confluence,
            pair=pair,
            safety_report=report,
            model=model,
            block_number=None,
            cooldown_minutes=settings.signal_cooldown_minutes,
        )
        ok, reason = passes_entry_filters(
            payload,
            min_liquidity_usd=settings.min_liquidity_usd,
            max_entry_mcap_usd=settings.max_entry_mcap_usd,
        )
        if not ok:
            log.debug("signal.filtered", token=confluence.token, reason=reason)
            continue

        if pair:
            await repo.upsert_tokens(
                chain.chain_id,
                [
                    {
                        "address": confluence.token,
                        "symbol": pair.base_symbol,
                        "name": pair.base_name,
                        "pair_address": pair.pair_address,
                        "dex_id": pair.dex_id,
                        "launched_at": pair.created_at,
                    }
                ],
            )

        stored = {k: v for k, v in payload.items() if not k.startswith("_")}
        signal_id = await repo.insert_signal(stored)
        if signal_id is not None:
            emitted += 1
            log.info(
                "signal.emitted",
                chain=chain.key,
                token=confluence.token,
                clusters=confluence.distinct_clusters,
                p2x=payload["quality_p2x"],
                verdict=payload["safety_verdict"],
            )
    return emitted


# --- stage 3: rescore wallets and refresh the watchlist -----------------


async def rescore(chain: Chain, settings: Settings) -> int:
    """Recompute wallet scores and replace the watchlist with the best."""
    history = await repo.wallet_trade_history(chain.chain_id, settings.score_window_days)
    if not history:
        return 0

    buys = [
        {"wallet": row["wallet"], "token": row["token"], "ts": row["ts"]}
        for row in history
        if row["side"] == "buy"
    ]
    cluster_result = clustering.build_clusters(buys)
    await repo.replace_clusters(chain.chain_id, cluster_result.mapping, "cobuy_timing")

    result = compute_round_trips(history)
    ranked = rank_wallets(
        group_round_trips(result.round_trips),
        now=datetime.now(UTC),
        halflife_days=settings.score_halflife_days,
        min_trades=settings.smart_wallet_min_trades,
        top_n=settings.smart_wallet_top_n,
    )
    await repo.upsert_scores(chain.chain_id, [s.as_row(settings.score_window_days) for s in ranked])

    smart = [s.wallet for s in ranked if s.is_smart]
    if smart:
        await repo.set_watchlist(chain.chain_id, smart)
    log.info(
        "pipeline.rescored",
        chain=chain.key,
        scored=len(ranked),
        smart=len(smart),
        sybil_clusters=len(cluster_result.multi_wallet_clusters),
    )
    return len(smart)


# --- stage 4: discover new candidate wallets ----------------------------


async def discover(
    rpc: RpcPool, chain: Chain, settings: Settings, client: DexScreenerClient, *, limit: int = 12
) -> int:
    pairs = await discovery.candidate_tokens(client, chain)
    if not pairs:
        return 0

    head = await rpc.block_number()
    found: set[str] = set()
    for pair in pairs[:limit]:
        if not pair.pair_address or pair.age_minutes is None:
            continue
        # Look at the token's first hours, where the informative buyers are.
        age_blocks = blocks_for_minutes(chain, pair.age_minutes)
        start = max(0, head - age_blocks)
        end = min(head, start + blocks_for_minutes(chain, 180))
        try:
            buyers = await discovery.early_buyers(
                rpc,
                chain,
                token=pair.base_address,
                pair_address=pair.pair_address,
                from_block=start,
                to_block=end,
            )
        except Exception as exc:
            log.warning("discovery.token_failed", token=pair.base_address, error=str(exc))
            continue
        found.update(buyers)

    if not found:
        return 0

    eoas, contracts = await discovery.filter_to_eoas(rpc, sorted(found))
    await repo.upsert_wallets(chain.chain_id, eoas)
    if contracts:
        await repo.upsert_wallets(chain.chain_id, contracts)
        await repo.mark_contracts(chain.chain_id, contracts)
    log.info("pipeline.discovered", chain=chain.key, wallets=len(eoas), contracts=len(contracts))
    return len(eoas)


# --- stage 5: keep outcomes current -------------------------------------


async def track_outcomes(chain: Chain, client: DexScreenerClient) -> int:
    return await outcomes.refresh(client, chain)


# --- model refresh ------------------------------------------------------


async def refit_quality_model(days: int = 90) -> quality.QualityModel:
    """Refit the quality model on resolved signals, or keep the prior."""
    rows = await repo.signals_for_backtest(days)
    samples = []
    for row in rows:
        peak = row.get("peak_multiple")
        if peak is None:
            continue
        samples.append(
            {
                "features": quality.build_features(
                    liquidity_usd=_f(row.get("snap_liquidity_usd")),
                    mcap_usd=_f(row.get("snap_mcap_usd")),
                    distinct_clusters=int(row.get("distinct_clusters") or 0),
                    age_minutes=row.get("snap_age_minutes"),
                    buys_24h=None,
                    sells_24h=None,
                    usd_spent=_f(row.get("usd_spent")),
                    safety_verdict=str(row.get("safety_verdict") or "unknown"),
                ),
                "reached_2x": float(peak) >= 2.0,
            }
        )
    model = quality.fit(samples)
    log.info(
        "quality.model", fitted=model.is_fitted, samples=len(samples), trained_on=model.trained_on
    )
    return model


def _f(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
