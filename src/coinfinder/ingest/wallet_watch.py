"""Wallet-centric indexer.

Indexing every swap on every pool is impossible on free RPC quotas. The way
around it is that ERC20 ``Transfer`` logs index both ``from`` and ``to``, and
``eth_getLogs`` accepts a *list* of values per topic position. So a single
query covers every token movement of every wallet we track:

    topics = [Transfer, null,      [w1, w2, ... wN]]   -> tokens received (buys)
    topics = [Transfer, [w1..wN],  null            ]   -> tokens sent     (sells)

Cost is therefore proportional to the number of tracked wallets, not to chain
activity, which is what makes the whole system viable without a paid provider.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import structlog

from coinfinder.chains import Chain
from coinfinder.ingest.extract import RawTrade, block_ts, extract_trades
from coinfinder.rpc import abi
from coinfinder.rpc.pool import RangeTooLarge, RpcPool

log = structlog.get_logger(__name__)

#: Never subdivide below this; a provider that cannot serve it is unusable.
MIN_RANGE = 8


def chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _get_logs_adaptive(
    rpc: RpcPool,
    *,
    from_block: int,
    to_block: int,
    topics: list,
    max_depth: int = 6,
) -> list[dict]:
    """Fetch logs, halving the window whenever a provider rejects the range."""
    try:
        return await rpc.get_logs(from_block=from_block, to_block=to_block, topics=topics)
    except RangeTooLarge:
        span = to_block - from_block
        if span < MIN_RANGE or max_depth <= 0:
            log.warning("ingest.range_floor", from_block=from_block, to_block=to_block)
            return []
        mid = from_block + span // 2
        left = await _get_logs_adaptive(
            rpc, from_block=from_block, to_block=mid, topics=topics, max_depth=max_depth - 1
        )
        right = await _get_logs_adaptive(
            rpc, from_block=mid + 1, to_block=to_block, topics=topics, max_depth=max_depth - 1
        )
        return left + right


async def find_wallet_activity(
    rpc: RpcPool,
    *,
    wallets: list[str],
    from_block: int,
    to_block: int,
    batch_size: int,
) -> dict[str, int]:
    """Return ``{tx_hash: block_number}`` for every tx touching these wallets."""
    touched: dict[str, int] = {}
    for group in chunk(sorted({w.lower() for w in wallets}), batch_size):
        padded = [abi.address_to_topic(w) for w in group]
        for topics in (
            [abi.TRANSFER, None, padded],  # received -> buys
            [abi.TRANSFER, padded, None],  # sent -> sells
        ):
            logs = await _get_logs_adaptive(
                rpc, from_block=from_block, to_block=to_block, topics=topics
            )
            for entry in logs:
                tx_hash = str(entry.get("transactionHash", "")).lower()
                if not tx_hash:
                    continue
                block_raw = entry.get("blockNumber")
                block = int(block_raw, 16) if isinstance(block_raw, str) else int(block_raw or 0)
                touched[tx_hash] = block
    return touched


async def fetch_transactions(
    rpc: RpcPool, tx_hashes: list[str], *, batch_size: int = 20
) -> dict[str, tuple[list[dict], int]]:
    """Fetch ``{tx_hash: (logs, value_wei)}`` for each transaction.

    Receipts give the log set; the transaction body gives ``value``, needed
    when a trader spent raw native ETH and the router wrapped it.
    """
    out: dict[str, tuple[list[dict], int]] = {}
    for group in chunk(tx_hashes, batch_size):
        receipts, bodies = await asyncio.gather(
            rpc.batch([("eth_getTransactionReceipt", [h]) for h in group]),
            rpc.batch([("eth_getTransactionByHash", [h]) for h in group]),
        )
        for tx_hash, receipt, body in zip(group, receipts, bodies, strict=True):
            if not isinstance(receipt, dict):
                continue
            # status "0x0" means the transaction reverted: no balances moved.
            if str(receipt.get("status", "0x1")).lower() in ("0x0", "0"):
                continue
            value_raw = (body or {}).get("value") if isinstance(body, dict) else None
            value_wei = int(value_raw, 16) if isinstance(value_raw, str) else 0
            out[tx_hash] = (receipt.get("logs") or [], value_wei)
    return out


async def scan_range(
    rpc: RpcPool,
    chain: Chain,
    *,
    wallets: list[str],
    from_block: int,
    to_block: int,
    batch_size: int,
) -> list[RawTrade]:
    """Full pipeline for one block window: activity -> receipts -> trades."""
    if not wallets or to_block < from_block:
        return []

    touched = await find_wallet_activity(
        rpc,
        wallets=wallets,
        from_block=from_block,
        to_block=to_block,
        batch_size=batch_size,
    )
    if not touched:
        return []

    tx_data = await fetch_transactions(rpc, list(touched))
    timestamps = await rpc.get_block_timestamps([touched[h] for h in tx_data if h in touched])

    wallet_set = {w.lower() for w in wallets}
    trades: list[RawTrade] = []
    for tx_hash, (logs, value_wei) in tx_data.items():
        block = touched[tx_hash]
        raw_ts = timestamps.get(block)
        if raw_ts is None:
            continue
        trades.extend(
            extract_trades(
                logs=logs,
                chain=chain,
                wallets=wallet_set,
                tx_hash=tx_hash,
                block_number=block,
                ts=block_ts(raw_ts),
                tx_value_wei=value_wei,
            )
        )

    log.info(
        "ingest.scanned",
        chain=chain.key,
        blocks=f"{from_block}-{to_block}",
        txs=len(tx_data),
        trades=len(trades),
    )
    return trades


def next_window(
    *, last_done: int, head: int, confirmations: int, max_span: int
) -> tuple[int, int] | None:
    """Pick the next safe block window, or None when we are caught up.

    Blocks within ``confirmations`` of the head are withheld so a reorg cannot
    leave us with trades that never happened.
    """
    safe_head = head - confirmations
    if safe_head <= last_done:
        return None
    start = last_done + 1
    return start, min(safe_head, start + max_span - 1)


def blocks_for_minutes(chain: Chain, minutes: float) -> int:
    return max(1, int(minutes * 60 / chain.block_time_seconds))


def utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
