"""The filter grid searched by the strategy leaderboard.

Kept small on purpose. Every extra dimension multiplies the number of
combinations tested, and the more combinations you rank by past performance,
the more certain it is that the winner is noise. 72 combinations against a few
thousand signals is already enough for the out-of-sample column to be the part
worth reading.
"""

from __future__ import annotations

from coinfinder.backtest.engine import FilterSpec
from coinfinder.chains import ALL_CHAINS

CLUSTER_STEPS = (3, 4, 5)
MCAP_CAPS: tuple[float | None, ...] = (None, 100_000.0, 500_000.0)
SAFETY_SETS: tuple[tuple[str, ...] | None, ...] = (None, ("safe", "caution"))


def chain_options() -> list[tuple[str, tuple[int, ...] | None]]:
    options: list[tuple[str, tuple[int, ...] | None]] = [("all chains", None)]
    options += [(chain.name, (chain.chain_id,)) for chain in ALL_CHAINS.values()]
    return options


def default_grid() -> list[FilterSpec]:
    specs: list[FilterSpec] = []
    for _, chain_ids in chain_options():
        for clusters in CLUSTER_STEPS:
            for cap in MCAP_CAPS:
                for verdicts in SAFETY_SETS:
                    specs.append(
                        FilterSpec(
                            chains=chain_ids,
                            min_clusters=clusters,
                            max_mcap_usd=cap,
                            safety_verdicts=verdicts,
                        )
                    )
    return specs
