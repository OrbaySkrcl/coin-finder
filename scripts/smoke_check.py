"""Verify the external services this system depends on are actually reachable.

The development sandbox this was built in blocks DexScreener, GeckoTerminal and
every public RPC at the network policy level, so live compatibility could not
be checked there. Run this once after the first deploy, where egress is open.

    railway run python scripts/smoke_check.py

Exits non-zero if any required dependency fails, so it can gate a deploy.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from coinfinder.chains import enabled_chains
from coinfinder.config import get_settings
from coinfinder.logging_setup import setup_logging
from coinfinder.rpc.erc20 import fetch_token_meta
from coinfinder.rpc.pool import RpcPool
from coinfinder.sources.dexscreener import DexScreenerClient, best_pair

OK, FAIL, WARN = "  OK  ", " FAIL ", " WARN "


def line(status: str, name: str, detail: str = "") -> None:
    print(f"[{status}] {name:<42} {detail}")


async def check_rpc(chain, settings) -> bool:
    urls = settings.rpc_override(chain.key) or list(chain.rpc_urls)
    healthy = 0
    async with RpcPool(urls, requests_per_second=3.0) as pool:
        for url in urls:
            single = RpcPool([url], requests_per_second=3.0, max_attempts=1)
            try:
                async with single:
                    block = await single.block_number()
                line(OK, f"RPC {chain.key}", f"{url} at block {block:,}")
                healthy += 1
            except Exception as exc:
                line(WARN, f"RPC {chain.key}", f"{url}: {type(exc).__name__}")

        if not healthy:
            line(FAIL, f"RPC {chain.key}", "no endpoint responded")
            return False

        # eth_getLogs with a topic list is the call the whole design rests on.
        try:
            head = await pool.block_number()
            from coinfinder.rpc import abi

            probe = (
                abi.address_to_topic(chain.wrapped_native)
                if int(chain.wrapped_native, 16)
                else None
            )
            await pool.get_logs(
                from_block=head - 20,
                to_block=head,
                topics=[abi.TRANSFER, None, [probe]] if probe else [abi.TRANSFER],
            )
            line(OK, f"eth_getLogs topic filter {chain.key}", "accepted")
        except Exception as exc:
            line(FAIL, f"eth_getLogs topic filter {chain.key}", str(exc)[:70])
            return False

        # Batched eth_call is how token metadata is read.
        if int(chain.wrapped_native, 16):
            try:
                meta = await fetch_token_meta(pool, [chain.wrapped_native])
                token = meta[chain.wrapped_native.lower()]
                line(OK, f"batch eth_call {chain.key}", f"{token.symbol} / {token.decimals}dp")
            except Exception as exc:
                line(WARN, f"batch eth_call {chain.key}", str(exc)[:70])
    return True


async def check_dexscreener(chains) -> bool:
    async with DexScreenerClient() as client:
        ok = True
        for chain in chains:
            if not int(chain.wrapped_native, 16):
                line(WARN, f"DexScreener {chain.key}", "no wrapped-native address configured")
                continue
            pairs = await client.pairs_for_tokens(chain.dexscreener_slug, [chain.wrapped_native])
            pair = best_pair(pairs, chain.wrapped_native)
            if pair and pair.price_usd:
                line(
                    OK,
                    f"DexScreener {chain.key}",
                    f"{pair.base_symbol} ${pair.price_usd:,.2f}, liq {pair.liquidity_usd:,.0f}",
                )
            else:
                line(FAIL, f"DexScreener {chain.key}", f"{len(pairs)} pairs, no usable price")
                ok = False

        boosted = await client.latest_boosted()
        if boosted:
            line(OK, "DexScreener discovery feed", f"{len(boosted)} boosted tokens")
        else:
            line(WARN, "DexScreener discovery feed", "empty - wallet discovery will be slow")
    return ok


async def main() -> None:
    setup_logging()
    settings = get_settings()
    chains = enabled_chains(settings.chain_keys)
    print(f"\nChecking live dependencies for: {', '.join(c.name for c in chains)}\n")

    results = [await check_rpc(chain, settings) for chain in chains]
    print()
    results.append(await check_dexscreener(chains))

    print()
    if all(results):
        print("All required dependencies reachable. Ingestion can run.\n")
    else:
        print("Some required dependencies failed. Ingestion will not produce signals.\n")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
