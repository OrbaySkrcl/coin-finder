"""Sybil clustering.

The signal is "N independent smart wallets bought this". If one person runs
five wallets, a naive counter reports five-wallet conviction for what is
actually one opinion - and that is the easiest way for a token team to
manufacture a signal.

Co-buying behaviour exposes this without any extra RPC calls: wallets under one
operator buy the same tokens within seconds of each other, repeatedly. Wallets
are unioned when they co-buy often enough *and* often relative to how much they
trade, so two independently active traders who both happened to catch three of
the same launches are not merged.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Two buys of one token this close together are treated as coordinated.
DEFAULT_WINDOW_SECONDS = 90
#: Absolute number of coordinated co-buys before a link is considered.
DEFAULT_MIN_COBUYS = 3
#: ...and that must also be this share of the smaller wallet's activity.
DEFAULT_MIN_SHARE = 0.5


class UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        parent = self._parent.setdefault(item, item)
        if parent != item:
            root = self.find(parent)
            self._parent[item] = root
            return root
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic root keeps cluster ids stable across runs.
            lo, hi = sorted((ra, rb))
            self._parent[hi] = lo

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for item in list(self._parent):
            out[self.find(item)].append(item)
        return {root: sorted(members) for root, members in out.items()}


@dataclass(slots=True)
class ClusterResult:
    #: wallet -> cluster id (the lexicographically smallest member)
    mapping: dict[str, str]
    #: cluster id -> members, only for clusters with more than one wallet
    multi_wallet_clusters: dict[str, list[str]]

    def cluster_of(self, wallet: str) -> str:
        return self.mapping.get(wallet, wallet)

    def distinct_clusters(self, wallets: list[str]) -> int:
        return len({self.cluster_of(w) for w in wallets})


def build_clusters(
    buys: list[dict[str, Any]],
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    min_cobuys: int = DEFAULT_MIN_COBUYS,
    min_share: float = DEFAULT_MIN_SHARE,
) -> ClusterResult:
    """Cluster wallets from buy events.

    Each buy needs ``wallet``, ``token`` and ``ts``.
    """
    by_token: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    trades_per_wallet: dict[str, int] = defaultdict(int)
    for buy in buys:
        by_token[buy["token"]].append((buy["ts"], buy["wallet"]))
        trades_per_wallet[buy["wallet"]] += 1

    cobuys: dict[tuple[str, str], int] = defaultdict(int)
    for events in by_token.values():
        events.sort()
        for i, (ts_i, wallet_i) in enumerate(events):
            for ts_j, wallet_j in events[i + 1 :]:
                if (ts_j - ts_i).total_seconds() > window_seconds:
                    break
                if wallet_i == wallet_j:
                    continue
                cobuys[tuple(sorted((wallet_i, wallet_j)))] += 1  # type: ignore[index]

    uf = UnionFind()
    for wallet in trades_per_wallet:
        uf.find(wallet)
    for (a, b), count in cobuys.items():
        if count < min_cobuys:
            continue
        smaller = min(trades_per_wallet[a], trades_per_wallet[b])
        if smaller and count / smaller >= min_share:
            uf.union(a, b)

    groups = uf.groups()
    mapping = {w: root for root, members in groups.items() for w in members}
    return ClusterResult(
        mapping=mapping,
        multi_wallet_clusters={r: m for r, m in groups.items() if len(m) > 1},
    )
