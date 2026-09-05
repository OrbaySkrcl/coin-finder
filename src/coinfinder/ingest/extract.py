"""Turn raw transaction logs into normalised buy/sell trades.

This is pure logic with no I/O, which makes it the one part of ingestion that
can be verified exhaustively offline.

How a swap is read
------------------
A DEX swap emits a set of ERC20 ``Transfer`` logs. For a wallet we track:

* the **asset leg** is the non-quote token whose balance changed;
* the **value leg** is the quote token (WETH/USDC/WBNB...) that moved the other
  way, or - when the trader spent raw native ETH and the router wrapped it -
  the transaction's ``value`` field.

Known approximation: for aggregator or multi-hop transactions that move more
than one asset, the value leg cannot be split between assets without a price
oracle, so those trades are recorded with ``native_amount=None``. Scoring
treats an unpriced trade as unusable rather than guessing, which keeps wallet
PnL honest at the cost of dropping a small number of trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from coinfinder.chains import Chain
from coinfinder.rpc import abi

NATIVE_DECIMALS = 18


@dataclass(slots=True)
class Transfer:
    token: str
    sender: str
    recipient: str
    value: int
    log_index: int


@dataclass(slots=True)
class RawTrade:
    wallet: str
    token: str
    side: str  # "buy" | "sell"
    token_amount: int
    #: Value leg in native units (ETH/BNB). None when it could not be resolved.
    native_amount: float | None
    tx_hash: str
    log_index: int
    block_number: int
    ts: datetime

    def usd_value(self, native_price_usd: float | None) -> float | None:
        if self.native_amount is None or native_price_usd is None:
            return None
        return self.native_amount * native_price_usd


def parse_transfers(logs: list[dict]) -> list[Transfer]:
    """Decode every ERC20 Transfer in a receipt, skipping ERC721s."""
    out: list[Transfer] = []
    for log in logs:
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != abi.TRANSFER:
            continue
        decoded = abi.decode_transfer(log)
        if decoded is None:
            continue
        sender, recipient, value = decoded
        out.append(
            Transfer(
                token=str(log.get("address", "")).lower(),
                sender=sender,
                recipient=recipient,
                value=value,
                log_index=int(str(log.get("logIndex", "0x0")), 16)
                if isinstance(log.get("logIndex"), str)
                else int(log.get("logIndex") or 0),
            )
        )
    return out


def _quote_set(chain: Chain) -> frozenset[str]:
    return chain.quote_tokens | {chain.wrapped_native.lower()}


def extract_trades(
    *,
    logs: list[dict],
    chain: Chain,
    wallets: set[str],
    tx_hash: str,
    block_number: int,
    ts: datetime,
    tx_value_wei: int = 0,
) -> list[RawTrade]:
    """Extract this transaction's trades for the wallets we care about."""
    transfers = parse_transfers(logs)
    if not transfers:
        return []

    quotes = _quote_set(chain)
    wallets = {w.lower() for w in wallets}

    # The deepest quote-token movement anywhere in the tx is the pool leg. It
    # is used when the wallet itself never held the quote token, which is the
    # normal case for native-ETH swaps routed through a universal router.
    pool_quote_leg = max(
        (t.value for t in transfers if t.token in quotes),
        default=0,
    )

    trades: list[RawTrade] = []
    for wallet in wallets:
        asset_flow: dict[str, int] = {}
        quote_flow = 0
        minted_or_burned: set[str] = set()
        first_log: dict[str, int] = {}

        for t in transfers:
            if wallet not in (t.sender, t.recipient):
                continue
            delta = t.value if t.recipient == wallet else -t.value
            if t.token in quotes:
                quote_flow += delta
                continue
            # Liquidity provisioning / LP tokens mint and burn against 0x0;
            # those are not trades and must not enter PnL.
            if abi.ZERO_ADDRESS in (t.sender, t.recipient):
                minted_or_burned.add(t.token)
            asset_flow[t.token] = asset_flow.get(t.token, 0) + delta
            first_log.setdefault(t.token, t.log_index)

        assets = {
            token: flow
            for token, flow in asset_flow.items()
            if flow != 0 and token not in minted_or_burned and token not in chain.ignored_addresses
        }
        if not assets:
            continue

        # Only a single-asset transaction lets us attribute the value leg.
        resolvable = len(assets) == 1
        for token, flow in assets.items():
            side = "buy" if flow > 0 else "sell"
            native_amount: float | None = None
            if resolvable:
                # Prefer the wallet's own quote movement; fall back to the
                # pool leg, then to raw native value for buys.
                raw = abs(quote_flow) or pool_quote_leg
                if raw == 0 and side == "buy":
                    raw = tx_value_wei
                if raw:
                    native_amount = raw / 10**NATIVE_DECIMALS
            trades.append(
                RawTrade(
                    wallet=wallet,
                    token=token,
                    side=side,
                    token_amount=abs(flow),
                    native_amount=native_amount,
                    tx_hash=tx_hash.lower(),
                    log_index=first_log[token],
                    block_number=block_number,
                    ts=ts,
                )
            )
    return trades


def block_ts(timestamp_hex_or_int: str | int) -> datetime:
    value = (
        int(timestamp_hex_or_int, 16)
        if isinstance(timestamp_hex_or_int, str)
        else int(timestamp_hex_or_int)
    )
    return datetime.fromtimestamp(value, tz=UTC)
