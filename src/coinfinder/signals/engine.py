"""Confluence detection and signal construction.

A signal fires when enough *independent* smart wallets buy the same token
inside a window. "Independent" is the important word: the count is over sybil
clusters, not raw addresses, so one operator's five wallets are one vote.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from coinfinder.chains import Chain
from coinfinder.scoring.clustering import ClusterResult
from coinfinder.signals import quality, safety
from coinfinder.sources.dexscreener import PairInfo

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Confluence:
    token: str
    wallets: list[str]
    clusters: list[str]
    first_buy_at: datetime
    last_buy_at: datetime
    usd_spent: float
    buys: list[dict[str, Any]] = field(default_factory=list)

    @property
    def distinct_wallets(self) -> int:
        return len(self.wallets)

    @property
    def distinct_clusters(self) -> int:
        return len(self.clusters)


def detect_confluence(
    buys: list[dict[str, Any]],
    *,
    clusters: ClusterResult,
    now: datetime,
    window_minutes: int,
    min_clusters: int,
) -> list[Confluence]:
    """Group recent buys by token and keep those with enough independent buyers.

    ``buys`` need ``token``, ``wallet``, ``ts`` and optionally ``usd_value``.
    """
    cutoff = now - timedelta(minutes=window_minutes)
    by_token: dict[str, list[dict[str, Any]]] = {}
    for buy in buys:
        if buy["ts"] < cutoff:
            continue
        by_token.setdefault(buy["token"], []).append(buy)

    out: list[Confluence] = []
    for token, token_buys in by_token.items():
        # Keep only each wallet's first buy so repeated adds by one wallet do
        # not inflate conviction.
        first_by_wallet: dict[str, dict[str, Any]] = {}
        for buy in sorted(token_buys, key=lambda b: b["ts"]):
            first_by_wallet.setdefault(buy["wallet"], buy)

        wallets = sorted(first_by_wallet)
        cluster_ids = sorted({clusters.cluster_of(w) for w in wallets})
        if len(cluster_ids) < min_clusters:
            continue

        timestamps = [b["ts"] for b in first_by_wallet.values()]
        out.append(
            Confluence(
                token=token,
                wallets=wallets,
                clusters=cluster_ids,
                first_buy_at=min(timestamps),
                last_buy_at=max(timestamps),
                usd_spent=sum(float(b.get("usd_value") or 0.0) for b in token_buys),
                buys=token_buys,
            )
        )
    return sorted(out, key=lambda c: (-c.distinct_clusters, c.token))


def dedupe_key(chain_id: int, token: str, clusters: int, bucket: datetime) -> str:
    """Stable key so the same conviction level in the same window fires once.

    Including the cluster count means a token going from 3 to 5 independent
    buyers *does* re-alert - that is new information, not a duplicate.
    """
    raw = f"{chain_id}:{token.lower()}:{clusters}:{bucket.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cooldown_bucket(ts: datetime, cooldown_minutes: int) -> datetime:
    """Floor a timestamp onto a cooldown-sized grid."""
    epoch_minutes = int(ts.timestamp() // 60)
    return datetime.fromtimestamp(
        (epoch_minutes - epoch_minutes % max(1, cooldown_minutes)) * 60, tz=UTC
    )


def build_signal_payload(
    *,
    chain: Chain,
    confluence: Confluence,
    pair: PairInfo | None,
    safety_report: safety.SafetyReport,
    model: quality.QualityModel,
    block_number: int | None,
    cooldown_minutes: int,
) -> dict[str, Any]:
    """Freeze everything known at signal time into an immutable row."""
    age = pair.age_minutes if pair else None
    features = quality.build_features(
        liquidity_usd=pair.liquidity_usd if pair else None,
        mcap_usd=pair.mcap_usd if pair else None,
        distinct_clusters=confluence.distinct_clusters,
        age_minutes=age,
        buys_24h=pair.buys_24h if pair else None,
        sells_24h=pair.sells_24h if pair else None,
        usd_spent=confluence.usd_spent,
        safety_verdict=str(safety_report.verdict),
    )
    p2x = model.predict_p2x(features)
    bucket = cooldown_bucket(confluence.last_buy_at, cooldown_minutes)

    return {
        "chain_id": chain.chain_id,
        "token": confluence.token,
        "ts": confluence.last_buy_at,
        "block_number": block_number,
        "dedupe_key": dedupe_key(
            chain.chain_id, confluence.token, confluence.distinct_clusters, bucket
        ),
        "distinct_wallets": confluence.distinct_wallets,
        "distinct_clusters": confluence.distinct_clusters,
        "wallets": confluence.wallets,
        "usd_spent": confluence.usd_spent,
        "snap_price_usd": pair.price_usd if pair else None,
        "snap_mcap_usd": pair.mcap_usd if pair else None,
        "snap_fdv_usd": pair.fdv_usd if pair else None,
        "snap_liquidity_usd": pair.liquidity_usd if pair else None,
        "snap_age_minutes": age,
        "snap_volume_24h_usd": pair.volume_24h_usd if pair else None,
        "snap_lp_locked_pct": safety_report.lp_burned_pct,
        "safety_flags": safety_report.flags,
        "safety_verdict": str(safety_report.verdict),
        "quality_score": quality.score_from_probability(p2x),
        "quality_p2x": round(p2x, 4),
        # Kept out of the DB row but useful to the caller for logging.
        "_features": features,
        "_model_fitted": model.is_fitted,
    }


def passes_entry_filters(
    payload: dict[str, Any],
    *,
    min_liquidity_usd: float,
    max_entry_mcap_usd: float,
) -> tuple[bool, str | None]:
    """Global gate applied before a signal is stored at all."""
    if payload["safety_verdict"] == str(safety.Verdict.DANGER):
        return False, "safety_danger"
    liq = payload.get("snap_liquidity_usd")
    if liq is not None and liq < min_liquidity_usd:
        return False, "liquidity_below_floor"
    mcap = payload.get("snap_mcap_usd")
    if mcap is not None and mcap > max_entry_mcap_usd:
        return False, "mcap_above_ceiling"
    if payload.get("snap_price_usd") in (None, 0):
        # Without an entry price the signal can never be scored or backtested.
        return False, "no_price"
    return True, None
