"""Smart-wallet discovery.

Chicken-and-egg: the watcher only follows wallets we already know about, but
those wallets have to come from somewhere. Discovery closes the loop by
working backwards from tokens that did well.

For each recent winner, one ``eth_getLogs`` bounded to the token contract
returns every transfer of it. Buyers are the addresses that received tokens
from the pool. Those become candidates; scoring then decides which of them
were early and profitable often enough to be worth following.

One targeted query per token keeps this inside free-tier quotas, which a
whole-chain scan never could.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from coinfinder.chains import Chain
from coinfinder.ingest.wallet_watch import _get_logs_adaptive
from coinfinder.rpc import abi
from coinfinder.rpc.erc20 import is_contract
from coinfinder.rpc.pool import RpcPool
from coinfinder.sources.dexscreener import DexScreenerClient, PairInfo

log = structlog.get_logger(__name__)

#: Buyers beyond this rank in a token's history are too late to be informative.
MAX_BUYERS_PER_TOKEN = 250


@dataclass(slots=True)
class Candidate:
    address: str
    tokens_bought: int = 0


async def candidate_tokens(
    client: DexScreenerClient, chain: Chain, *, min_liquidity_usd: float = 20_000.0
) -> list[PairInfo]:
    """Recent tokens worth mining for early buyers.

    Uses the free discovery endpoints. Boosted tokens are paid placements, so
    they are a stream of *fresh launches*, not a quality signal - the filtering
    below is what makes them useful.
    """
    seen: dict[str, PairInfo] = {}
    boosted = await client.latest_boosted()
    addresses = [
        str(item.get("tokenAddress", "")).lower()
        for item in boosted
        if str(item.get("chainId", "")).lower() == chain.dexscreener_slug
    ]
    for start in range(0, len(addresses), 30):
        for pair in await client.pairs_for_tokens(
            chain.dexscreener_slug, addresses[start : start + 30]
        ):
            if (pair.liquidity_usd or 0) < min_liquidity_usd:
                continue
            existing = seen.get(pair.base_address)
            if existing is None or (pair.liquidity_usd or 0) > (existing.liquidity_usd or 0):
                seen[pair.base_address] = pair
    return list(seen.values())


async def early_buyers(
    rpc: RpcPool,
    chain: Chain,
    *,
    token: str,
    pair_address: str,
    from_block: int,
    to_block: int,
) -> list[str]:
    """Addresses that bought ``token`` out of its pool in a block window."""
    logs = await _get_logs_adaptive(
        rpc,
        from_block=from_block,
        to_block=to_block,
        topics=[abi.TRANSFER, abi.address_to_topic(pair_address), None],
    )
    buyers: list[str] = []
    seen: set[str] = set()
    for entry in logs:
        if str(entry.get("address", "")).lower() != token.lower():
            continue
        decoded = abi.decode_transfer(entry)
        if decoded is None:
            continue
        _, recipient, _ = decoded
        if recipient in seen or recipient in chain.ignored_addresses:
            continue
        if recipient == abi.ZERO_ADDRESS or recipient == pair_address.lower():
            continue
        seen.add(recipient)
        buyers.append(recipient)
        if len(buyers) >= MAX_BUYERS_PER_TOKEN:
            break
    return buyers


async def filter_to_eoas(rpc: RpcPool, addresses: list[str]) -> tuple[list[str], list[str]]:
    """Split candidates into wallets and contracts.

    Contracts here are routers, aggregators and bot proxies. Scoring them would
    put infrastructure at the top of every leaderboard.
    """
    flags = await is_contract(rpc, addresses)
    eoas = [a for a in addresses if not flags.get(a, False)]
    contracts = [a for a in addresses if flags.get(a, False)]
    return eoas, contracts
