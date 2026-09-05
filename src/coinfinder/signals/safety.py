"""Token safety checks that work on free public RPC.

Proper honeypot detection needs `eth_call` state overrides or a deployed
simulator contract, and most free endpoints refuse both. Rather than ship a
fake "verified safe" badge, this module uses checks that genuinely work for
free and is explicit about what it cannot see.

The strongest free signal is simply **whether anyone has managed to sell**. A
honeypot produces buys and no sells; observed sells are direct evidence that
exiting works. That is combined with LP burn/lock state and ownership.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from coinfinder.chains import Chain
from coinfinder.rpc import abi
from coinfinder.rpc.pool import RpcPool

log = structlog.get_logger(__name__)


class Verdict(StrEnum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGER = "danger"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class SafetyReport:
    verdict: Verdict = Verdict.UNKNOWN
    flags: list[str] = field(default_factory=list)
    lp_burned_pct: float | None = None
    owner_renounced: bool | None = None
    sells_observed: int | None = None
    liquidity_ratio: float | None = None
    #: True when the chain has no third-party risk tooling at all.
    limited_coverage: bool = False

    @property
    def is_blocking(self) -> bool:
        return self.verdict is Verdict.DANGER


@dataclass(slots=True)
class OnchainChecks:
    """Result of the free on-chain reads. Typed so callers cannot mix the fields."""

    lp_burned_pct: float | None = None
    owner_renounced: bool | None = None


async def onchain_checks(
    rpc: RpcPool, chain: Chain, *, token: str, pair_address: str | None
) -> OnchainChecks:
    """Read LP burn share and ownership renouncement. Two to four eth_calls."""
    out = OnchainChecks()

    calls: list[tuple[str, list]] = [
        ("eth_call", [{"to": token, "data": abi.SEL_OWNER}, "latest"]),
    ]
    if pair_address:
        calls += [
            ("eth_call", [{"to": pair_address, "data": abi.SEL_TOTAL_SUPPLY}, "latest"]),
            (
                "eth_call",
                [
                    {
                        "to": pair_address,
                        "data": abi.encode_call(
                            "balanceOf(address)", ["address"], [abi.ZERO_ADDRESS]
                        ),
                    },
                    "latest",
                ],
            ),
        ]

    try:
        results = await rpc.batch(calls)
    except Exception as exc:
        log.warning("safety.rpc_failed", token=token, error=str(exc))
        return out

    owner_raw = results[0]
    if owner_raw and owner_raw != "0x":
        owner = abi.topic_to_address(owner_raw)
        out.owner_renounced = owner == abi.ZERO_ADDRESS
    # A token with no owner() at all is fine; many launchpad tokens omit it.

    if pair_address and len(results) >= 3:
        supply = abi.decode_uint(results[1])
        burned = abi.decode_uint(results[2])
        if supply and burned is not None:
            out.lp_burned_pct = round(100.0 * burned / supply, 2)
    return out


def assess(
    *,
    chain: Chain,
    sells_observed: int | None,
    buys_observed: int | None,
    liquidity_usd: float | None,
    mcap_usd: float | None,
    lp_burned_pct: float | None,
    owner_renounced: bool | None,
    min_liquidity_usd: float,
    age_minutes: int | None = None,
) -> SafetyReport:
    """Combine the free checks into one verdict."""
    report = SafetyReport(
        lp_burned_pct=lp_burned_pct,
        owner_renounced=owner_renounced,
        sells_observed=sells_observed,
        limited_coverage=not chain.risk_data_available,
    )
    flags = report.flags

    if not chain.risk_data_available:
        flags.append("no_risk_tooling_on_chain")

    # --- blocking conditions -------------------------------------------
    # Buys with zero sells is the classic honeypot fingerprint. It only counts
    # once there are enough buys for the absence of sells to mean something.
    if sells_observed == 0 and (buys_observed or 0) >= 15:
        flags.append("no_sells_despite_many_buys")
        report.verdict = Verdict.DANGER
        return report

    if liquidity_usd is not None and liquidity_usd < min_liquidity_usd:
        flags.append("liquidity_below_floor")
        report.verdict = Verdict.DANGER
        return report

    # --- caution conditions --------------------------------------------
    if liquidity_usd and mcap_usd:
        ratio = liquidity_usd / mcap_usd
        report.liquidity_ratio = round(ratio, 4)
        # Thin liquidity against a large cap means the exit is far more
        # expensive than the quoted price suggests.
        if ratio < 0.02:
            flags.append("liquidity_under_2pct_of_mcap")

    if lp_burned_pct is not None and lp_burned_pct < 50.0:
        flags.append("lp_not_burned")
    if owner_renounced is False:
        flags.append("owner_not_renounced")
    if age_minutes is not None and age_minutes < 10:
        flags.append("very_new_token")
    if sells_observed is None:
        flags.append("sell_activity_unknown")

    if not chain.risk_data_available:
        # Nothing can be confirmed here, so never claim safety.
        report.verdict = Verdict.CAUTION
        return report

    blocking_flags = {"liquidity_under_2pct_of_mcap", "lp_not_burned"}
    if blocking_flags & set(flags):
        report.verdict = Verdict.CAUTION
    elif flags:
        report.verdict = Verdict.CAUTION if len(flags) > 1 else Verdict.SAFE
    else:
        report.verdict = Verdict.SAFE
    return report
