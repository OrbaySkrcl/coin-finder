"""Chain registry.

Adding a chain must never require touching ingestion code: everything the
indexer needs (endpoints, wrapped-native address, DEX factories, stablecoins,
router/aggregator addresses to ignore) lives here as data.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Chain:
    key: str
    chain_id: int
    name: str
    #: Fits a Telegram button three-to-a-row on a phone, where "Robinhood
    #: Chain" is truncated to nonsense.
    short_name: str
    native_symbol: str
    wrapped_native: str
    #: Public RPC endpoints, tried in order with health-based rotation.
    rpc_urls: tuple[str, ...]
    explorer: str
    #: DexScreener's identifier for this chain (used by their REST API).
    dexscreener_slug: str
    #: GeckoTerminal's network slug. Empty when unsupported.
    geckoterminal_slug: str
    #: Tokens treated as "quote" side of a pair - a swap into these is a SELL.
    quote_tokens: frozenset[str]
    #: Average block time, used to convert time windows into block ranges.
    block_time_seconds: float
    #: Contracts that must never be scored as wallets (routers, aggregators,
    #: known MEV/trading bots). Lower-case.
    ignored_addresses: frozenset[str] = field(default_factory=frozenset)
    #: Approximate USD price of a simple swap, for the backtest cost model.
    typical_swap_gas_usd: float = 0.05
    #: Set when the chain has no reliable third-party risk/tax data yet.
    risk_data_available: bool = True


def _lc(*addrs: str) -> frozenset[str]:
    return frozenset(a.lower() for a in addrs)


BASE = Chain(
    key="base",
    chain_id=8453,
    name="Base",
    short_name="Base",
    native_symbol="ETH",
    wrapped_native="0x4200000000000000000000000000000000000006",
    rpc_urls=(
        "https://mainnet.base.org",
        "https://base-rpc.publicnode.com",
        "https://base.llamarpc.com",
        "https://base.drpc.org",
        "https://1rpc.io/base",
    ),
    explorer="https://basescan.org",
    dexscreener_slug="base",
    geckoterminal_slug="base",
    quote_tokens=_lc(
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    ),
    block_time_seconds=2.0,
    ignored_addresses=_lc(
        "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap V3 SwapRouter02
        "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Uniswap V4 UniversalRouter
        "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
        "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x Exchange Proxy
        "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43",  # Aerodrome Router
        "0x6cb442acf35158d5eda88fe602221b67b400be3e",  # Aerodrome universal
    ),
    typical_swap_gas_usd=0.02,
)

ROBINHOOD = Chain(
    key="robinhood",
    chain_id=4663,
    name="Robinhood Chain",
    short_name="Robinhood",
    native_symbol="ETH",
    # Arbitrum Orbit chains expose WETH at the standard Orbit predeploy slot;
    # override via env if the canonical deployment differs.
    wrapped_native="0x0000000000000000000000000000000000000000",
    rpc_urls=("https://rpc.mainnet.chain.robinhood.com",),
    explorer="https://robinhoodchain.blockscout.com",
    dexscreener_slug="robinhood",
    geckoterminal_slug="",
    quote_tokens=frozenset(),
    block_time_seconds=0.25,
    ignored_addresses=frozenset(),
    typical_swap_gas_usd=0.01,
    # No third-party honeypot/tax scanner covers this chain yet. The signal
    # engine downgrades quality and the bot prints an explicit DYOR warning.
    risk_data_available=False,
)

BSC = Chain(
    key="bsc",
    chain_id=56,
    name="BNB Chain",
    short_name="BNB",
    native_symbol="BNB",
    wrapped_native="0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    rpc_urls=(
        "https://bsc-rpc.publicnode.com",
        "https://binance.llamarpc.com",
        "https://bsc.drpc.org",
        "https://bsc-dataseed1.bnbchain.org",
        "https://1rpc.io/bnb",
    ),
    explorer="https://bscscan.com",
    dexscreener_slug="bsc",
    geckoterminal_slug="bsc",
    quote_tokens=_lc(
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
    ),
    block_time_seconds=0.75,
    ignored_addresses=_lc(
        "0x10ed43c718714eb63d5aa57b78b54704e256024e",  # PancakeSwap V2 Router
        "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",  # PancakeSwap SmartRouter
        "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
        "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x Exchange Proxy
    ),
    typical_swap_gas_usd=0.12,
)

ALL_CHAINS: dict[str, Chain] = {c.key: c for c in (BASE, ROBINHOOD, BSC)}
BY_CHAIN_ID: dict[int, Chain] = {c.chain_id: c for c in ALL_CHAINS.values()}


def get_chain(key: str) -> Chain:
    try:
        return ALL_CHAINS[key.lower()]
    except KeyError as exc:  # pragma: no cover - programmer error
        raise KeyError(f"unknown chain {key!r}; known: {sorted(ALL_CHAINS)}") from exc


def enabled_chains(keys: list[str]) -> list[Chain]:
    return [get_chain(k) for k in keys]
