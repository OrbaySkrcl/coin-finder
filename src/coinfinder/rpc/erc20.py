"""ERC20 metadata and balance reads, batched to stay inside free-tier limits."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from coinfinder.rpc import abi
from coinfinder.rpc.pool import RpcPool

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class TokenMeta:
    address: str
    symbol: str | None = None
    name: str | None = None
    decimals: int = 18
    total_supply: int | None = None

    @property
    def supply_float(self) -> float | None:
        if self.total_supply is None:
            return None
        return self.total_supply / (10**self.decimals)


async def fetch_token_meta(rpc: RpcPool, addresses: list[str]) -> dict[str, TokenMeta]:
    """Read symbol/name/decimals/totalSupply for many tokens in one batch.

    Tokens that revert on any call still get a record, with whatever fields
    did decode - malformed ERC20s are common in this corner of the market and
    must not abort the whole batch.
    """
    addresses = [a.lower() for a in dict.fromkeys(addresses)]
    if not addresses:
        return {}

    calls: list[tuple[str, list]] = []
    for addr in addresses:
        for sel in (abi.SEL_SYMBOL, abi.SEL_NAME, abi.SEL_DECIMALS, abi.SEL_TOTAL_SUPPLY):
            calls.append(("eth_call", [{"to": addr, "data": sel}, "latest"]))

    results = await rpc.batch(calls)
    out: dict[str, TokenMeta] = {}
    for i, addr in enumerate(addresses):
        sym, name, dec, supply = results[i * 4 : i * 4 + 4]
        decimals = abi.decode_uint(dec)
        out[addr] = TokenMeta(
            address=addr,
            symbol=abi.decode_string(sym),
            name=abi.decode_string(name),
            # Anything outside 0..36 is a broken token reporting nonsense.
            decimals=decimals if decimals is not None and 0 <= decimals <= 36 else 18,
            total_supply=abi.decode_uint(supply),
        )
    return out


async def fetch_balances(rpc: RpcPool, token: str, holders: list[str]) -> dict[str, int]:
    """Read one token's balance for many holders."""
    holders = [h.lower() for h in dict.fromkeys(holders)]
    if not holders:
        return {}
    calls = [
        (
            "eth_call",
            [
                {
                    "to": token.lower(),
                    "data": abi.encode_call("balanceOf(address)", ["address"], [h]),
                },
                "latest",
            ],
        )
        for h in holders
    ]
    results = await rpc.batch(calls)
    return {
        h: (abi.decode_uint(r) or 0) for h, r in zip(holders, results, strict=True) if r is not None
    }


async def is_contract(rpc: RpcPool, addresses: list[str]) -> dict[str, bool]:
    """Distinguish EOAs from contracts.

    Contracts must never be scored as smart wallets: routers, aggregators and
    trading-bot proxies would otherwise dominate every ranking.
    """
    addresses = [a.lower() for a in dict.fromkeys(addresses)]
    if not addresses:
        return {}
    results = await rpc.batch([("eth_getCode", [a, "latest"]) for a in addresses])
    return {
        a: bool(code and code not in ("0x", "0x0"))
        for a, code in zip(addresses, results, strict=True)
    }
