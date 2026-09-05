"""Exit models.

Every model declares whether it needs information a trader could not have had
at decision time. That flag is the whole point of the module: the reference
product's headline number ("exit at 50% of peak") is only reachable with
hindsight, and presenting it beside a realistic take-profit without saying so
is what makes such backtests misleading.

Classification used here:

``uses_look_ahead=False``
    Reproducible with orders placed at entry - a limit sell at 3x fills when
    the price touches 3x, no foresight required. Time stops likewise.

``uses_look_ahead=True``
    Requires knowing the future path: the peak, or where the peak was.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

HORIZONS = ("15m", "1h", "4h", "24h", "7d")


@dataclass(frozen=True, slots=True)
class Outcome:
    """What is known about a signal's price path after the fact."""

    peak_multiple: float | None
    current_multiple: float | None
    is_dead: bool = False
    horizons: dict[str, float | None] | None = None

    def horizon(self, name: str) -> float | None:
        return (self.horizons or {}).get(name)

    @property
    def final(self) -> float:
        """Where the position stands now. A dead token is worth nothing."""
        if self.is_dead:
            return 0.0
        return max(0.0, self.current_multiple if self.current_multiple is not None else 0.0)

    @property
    def peak(self) -> float:
        if self.peak_multiple is not None:
            return max(self.peak_multiple, self.final)
        return self.final


class ExitModel(Protocol):
    name: str
    uses_look_ahead: bool

    def gross_multiple(self, outcome: Outcome) -> float: ...


@dataclass(frozen=True, slots=True)
class HoldToNow:
    """Never sell. The honest reality check - and usually a grim one."""

    name: str = "hold_to_now"
    uses_look_ahead: bool = False

    def gross_multiple(self, outcome: Outcome) -> float:
        return outcome.final


@dataclass(frozen=True, slots=True)
class FixedTakeProfit:
    """Limit sell at ``target``; whatever does not fill rides to now."""

    target: float
    name: str = ""
    uses_look_ahead: bool = False

    def gross_multiple(self, outcome: Outcome) -> float:
        return self.target if outcome.peak >= self.target else outcome.final

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"tp_{self.target:g}x")


@dataclass(frozen=True, slots=True)
class Ladder:
    """Scale out across several targets, remainder held to now.

    Realistic: each rung is a resting limit order placed at entry.
    """

    rungs: tuple[tuple[float, float], ...] = ((2.0, 0.5), (5.0, 0.25), (10.0, 0.15))
    name: str = "ladder"
    uses_look_ahead: bool = False

    def gross_multiple(self, outcome: Outcome) -> float:
        realised = 0.0
        remaining = 1.0
        for target, fraction in self.rungs:
            take = min(fraction, remaining)
            if take <= 0:
                break
            if outcome.peak >= target:
                realised += take * target
                remaining -= take
        return realised + remaining * outcome.final


@dataclass(frozen=True, slots=True)
class TimeStop:
    """Sell at a fixed horizon after entry, regardless of price."""

    horizon: str
    name: str = ""
    uses_look_ahead: bool = False

    def gross_multiple(self, outcome: Outcome) -> float:
        value = outcome.horizon(self.horizon)
        return outcome.final if value is None else max(0.0, value)

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"time_{self.horizon}")


@dataclass(frozen=True, slots=True)
class TrailingStop:
    """Exit ``drop_pct`` below the running peak.

    Approximate, and optimistic: with only a peak and a final value there is no
    way to know whether the trail was hit earlier on the way up. Treated as
    look-ahead so it is never presented as an achievable result.
    """

    drop_pct: float = 0.35
    name: str = ""
    uses_look_ahead: bool = True

    def gross_multiple(self, outcome: Outcome) -> float:
        keep = 1.0 - self.drop_pct
        exit_at = outcome.peak * keep
        return exit_at if exit_at > outcome.final else outcome.final

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"trail_{int(self.drop_pct * 100)}pct")


@dataclass(frozen=True, slots=True)
class PeakFraction:
    """Sell at a fraction of the peak. Pure hindsight - shown only as a ceiling.

    This is the model behind the reference product's headline ROI. It is
    included so the gap between it and a realistic exit is visible, never as a
    default.
    """

    fraction: float = 0.5
    name: str = ""
    uses_look_ahead: bool = True

    def gross_multiple(self, outcome: Outcome) -> float:
        return max(outcome.peak * self.fraction, 0.0)

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", f"peak_{int(self.fraction * 100)}pct")


#: Models offered in the Strategy Lab. Realistic ones first, by design.
#: Protocol members are structurally typed, so the concrete tuple needs a cast.
DEFAULT_MODELS: tuple[ExitModel, ...] = cast(
    "tuple[ExitModel, ...]",
    (
        FixedTakeProfit(2.0),
        FixedTakeProfit(3.0),
        FixedTakeProfit(5.0),
        Ladder(),
        TimeStop("1h"),
        TimeStop("4h"),
        TimeStop("24h"),
        HoldToNow(),
        TrailingStop(0.35),
        PeakFraction(0.5),
    ),
)


def by_name(name: str) -> ExitModel:
    for model in DEFAULT_MODELS:
        if model.name == name:
            return model
    raise KeyError(f"unknown exit model {name!r}")


def realistic_models() -> tuple[ExitModel, ...]:
    return tuple(m for m in DEFAULT_MODELS if not m.uses_look_ahead)
