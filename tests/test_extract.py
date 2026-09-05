"""Exhaustive tests for swap extraction - pure logic, no network."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coinfinder.chains import BASE
from coinfinder.ingest.extract import extract_trades, parse_transfers
from coinfinder.rpc import abi

WETH = BASE.wrapped_native.lower()
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOKEN = "0x" + "aa" * 20
TOKEN2 = "0x" + "cc" * 20
WALLET = "0x" + "11" * 20
POOL = "0x" + "99" * 20
ROUTER = next(iter(BASE.ignored_addresses))
TS = datetime(2026, 9, 1, tzinfo=UTC)


def xfer(token, sender, recipient, value, log_index=0):
    return {
        "address": token,
        "topics": [abi.TRANSFER, abi.address_to_topic(sender), abi.address_to_topic(recipient)],
        "data": "0x" + int(value).to_bytes(32, "big").hex(),
        "logIndex": hex(log_index),
    }


def run(logs, tx_value_wei=0, wallets=(WALLET,)):
    return extract_trades(
        logs=logs,
        chain=BASE,
        wallets=set(wallets),
        tx_hash="0xDEAD",
        block_number=100,
        ts=TS,
        tx_value_wei=tx_value_wei,
    )


def test_weth_buy_is_detected_with_value_leg():
    logs = [
        xfer(WETH, WALLET, POOL, 4 * 10**17, 0),  # wallet pays 0.4 WETH
        xfer(TOKEN, POOL, WALLET, 2000 * 10**18, 1),  # receives 2000 TOKEN
    ]
    (trade,) = run(logs)
    assert trade.side == "buy"
    assert trade.token == TOKEN
    assert trade.token_amount == 2000 * 10**18
    assert trade.native_amount == pytest.approx(0.4)
    assert trade.usd_value(3000.0) == pytest.approx(1200.0)


def test_sell_is_detected():
    logs = [
        xfer(TOKEN, WALLET, POOL, 500 * 10**18, 0),
        xfer(WETH, POOL, WALLET, 10**18, 1),
    ]
    (trade,) = run(logs)
    assert trade.side == "sell"
    assert trade.native_amount == pytest.approx(1.0)


def test_native_eth_buy_uses_tx_value_when_wallet_never_holds_weth():
    # Universal router wraps ETH itself: the wallet has no WETH transfer.
    logs = [xfer(TOKEN, POOL, WALLET, 100 * 10**18, 0)]
    (trade,) = run(logs, tx_value_wei=25 * 10**16)  # 0.25 ETH
    assert trade.side == "buy"
    assert trade.native_amount == pytest.approx(0.25)


def test_pool_leg_used_when_router_holds_the_quote_token():
    # Wallet gets TOKEN; the WETH leg moves router -> pool, not via the wallet.
    logs = [
        xfer(WETH, ROUTER, POOL, 3 * 10**17, 0),
        xfer(TOKEN, POOL, WALLET, 100 * 10**18, 1),
    ]
    (trade,) = run(logs)
    assert trade.native_amount == pytest.approx(0.3)


def test_usdc_counts_as_quote_not_as_traded_asset():
    logs = [
        xfer(USDC, WALLET, POOL, 500 * 10**6, 0),
        xfer(TOKEN, POOL, WALLET, 10 * 10**18, 1),
    ]
    (trade,) = run(logs)
    assert trade.token == TOKEN  # not USDC


def test_lp_mint_is_not_a_trade():
    # Adding liquidity: LP token minted from the zero address.
    logs = [
        xfer(TOKEN, WALLET, POOL, 10**18, 0),
        xfer(POOL, abi.ZERO_ADDRESS, WALLET, 10**18, 1),
    ]
    trades = run(logs)
    assert all(t.token != POOL for t in trades)


def test_lp_burn_of_asset_is_excluded():
    logs = [xfer(TOKEN, abi.ZERO_ADDRESS, WALLET, 10**18, 0)]
    assert run(logs) == []


def test_router_addresses_are_never_traded_assets():
    logs = [
        xfer(WETH, WALLET, POOL, 10**17, 0),
        xfer(ROUTER, POOL, WALLET, 10**18, 1),
    ]
    assert run(logs) == []


def test_untracked_wallets_are_ignored():
    other = "0x" + "77" * 20
    logs = [xfer(TOKEN, POOL, other, 10**18, 0)]
    assert run(logs) == []


def test_multi_asset_tx_records_trades_without_a_value_leg():
    # Aggregator splits one payment across two tokens: attribution is
    # impossible without prices, so native_amount stays None by design.
    logs = [
        xfer(WETH, WALLET, POOL, 10**18, 0),
        xfer(TOKEN, POOL, WALLET, 10 * 10**18, 1),
        xfer(TOKEN2, POOL, WALLET, 20 * 10**18, 2),
    ]
    trades = run(logs)
    assert len(trades) == 2
    assert all(t.native_amount is None for t in trades)
    assert all(t.usd_value(3000.0) is None for t in trades)


def test_round_trip_in_one_tx_nets_to_nothing():
    logs = [
        xfer(TOKEN, POOL, WALLET, 10**18, 0),
        xfer(TOKEN, WALLET, POOL, 10**18, 1),
    ]
    assert run(logs) == []


def test_erc721_transfers_are_skipped():
    log = {
        "address": TOKEN,
        "topics": [
            abi.TRANSFER,
            abi.address_to_topic(POOL),
            abi.address_to_topic(WALLET),
            "0x" + "00" * 31 + "01",
        ],
        "data": "0x",
        "logIndex": "0x0",
    }
    assert parse_transfers([log]) == []
    assert run([log]) == []


def test_non_transfer_logs_are_ignored():
    assert parse_transfers([{"address": POOL, "topics": [abi.V2_SWAP], "data": "0x"}]) == []


def test_multiple_wallets_in_one_tx_each_get_a_trade():
    w2 = "0x" + "22" * 20
    logs = [
        xfer(WETH, WALLET, POOL, 10**18, 0),
        xfer(TOKEN, POOL, WALLET, 10**18, 1),
        xfer(WETH, w2, POOL, 2 * 10**18, 2),
        xfer(TOKEN, POOL, w2, 5 * 10**18, 3),
    ]
    trades = run(logs, wallets=(WALLET, w2))
    assert {t.wallet for t in trades} == {WALLET, w2}
    assert {round(t.native_amount, 4) for t in trades} == {1.0, 2.0}


def test_integer_log_index_is_accepted():
    log = xfer(TOKEN, POOL, WALLET, 10**18, 0)
    log["logIndex"] = 7
    (trade,) = run([log], tx_value_wei=10**17)
    assert trade.log_index == 7
