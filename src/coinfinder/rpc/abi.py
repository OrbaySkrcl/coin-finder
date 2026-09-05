"""Event signatures, topic helpers and ABI decoding.

Topic hashes are derived with keccak at import time rather than hard-coded, so
a typo becomes impossible.
"""

from __future__ import annotations

from typing import Any

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, keccak

ZERO_ADDRESS = "0x" + "0" * 40


def event_topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


def selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


# --- events ------------------------------------------------------------
TRANSFER = event_topic("Transfer(address,address,uint256)")
V2_SWAP = event_topic("Swap(address,uint256,uint256,uint256,uint256,address)")
V3_SWAP = event_topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
V2_PAIR_CREATED = event_topic("PairCreated(address,address,address,uint256)")
V3_POOL_CREATED = event_topic("PoolCreated(address,address,uint24,int24,address)")

# --- function selectors ------------------------------------------------
SEL_SYMBOL = selector("symbol()")
SEL_NAME = selector("name()")
SEL_DECIMALS = selector("decimals()")
SEL_TOTAL_SUPPLY = selector("totalSupply()")
SEL_BALANCE_OF = selector("balanceOf(address)")
SEL_OWNER = selector("owner()")


def address_to_topic(address: str) -> str:
    """Left-pad a 20-byte address into a 32-byte log topic."""
    clean = address.lower().removeprefix("0x")
    if len(clean) != 40:
        raise ValueError(f"not an address: {address!r}")
    return "0x" + "0" * 24 + clean


def topic_to_address(topic: str) -> str:
    return "0x" + topic.lower().removeprefix("0x")[-40:]


def encode_call(sig: str, arg_types: list[str], args: list[Any]) -> str:
    """Build calldata for an ``eth_call``."""
    data = selector(sig).removeprefix("0x")
    if arg_types:
        data += abi_encode(arg_types, args).hex()
    return "0x" + data


def decode_uint(hex_data: str | None) -> int | None:
    if not hex_data or hex_data == "0x":
        return None
    try:
        return int(hex_data, 16)
    except ValueError:
        return None


def decode_string(hex_data: str | None) -> str | None:
    """Decode a returned string, tolerating the bytes32 form old tokens use."""
    if not hex_data or hex_data == "0x":
        return None
    raw = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(raw) >= 64:
        try:
            return abi_decode(["string"], raw)[0].strip("\x00") or None
        except Exception:
            pass
    # bytes32-style: a fixed 32-byte word padded with NULs.
    try:
        return raw.rstrip(b"\x00").decode("utf-8", errors="ignore").strip("\x00") or None
    except Exception:
        return None


def decode_transfer(log: dict[str, Any]) -> tuple[str, str, int] | None:
    """Decode an ERC20 Transfer log into ``(from, to, value)``.

    Returns ``None`` for ERC721 Transfers, which carry a third indexed topic
    (the token id) and no data.
    """
    topics = log.get("topics") or []
    if len(topics) != 3:
        return None
    value = decode_uint(log.get("data"))
    if value is None:
        return None
    return topic_to_address(topics[1]), topic_to_address(topics[2]), value


def normalise_address(value: str) -> str:
    return value.lower()
