"""Calibrated signal quality.

The reference product prints a star rating. Stars are unfalsifiable - nobody
can tell whether three stars was right. This module outputs a **probability**
that the token reaches 2x before it dies, which can be checked against what
actually happened and is therefore the only honest form of the claim.

Bootstrapping problem: on day one there is no outcome data to fit on. Rather
than pretend, the module ships an explicit *prior* whose coefficients encode
well-known relationships (deeper liquidity is safer, more independent buyers
is better, a huge entry cap leaves less room), and marks its output
``is_fitted=False``. Once enough resolved signals exist, ``fit()`` replaces
those coefficients with ones learned from real outcomes and the flag flips.
Nothing in the UI should present prior-based numbers as measured performance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Resolved signals needed before a fitted model beats the prior.
MIN_SAMPLES_TO_FIT = 400


@dataclass(slots=True)
class QualityModel:
    intercept: float
    coefficients: dict[str, float]
    is_fitted: bool = False
    trained_on: int = 0
    feature_means: dict[str, float] = field(default_factory=dict)

    def predict_p2x(self, features: dict[str, float]) -> float:
        z = self.intercept + sum(
            weight * features.get(name, self.feature_means.get(name, 0.0))
            for name, weight in self.coefficients.items()
        )
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


# Prior coefficients. Directions are deliberate and defensible; magnitudes are
# modest so the prior never produces confident-looking extremes.
PRIOR = QualityModel(
    intercept=-1.10,  # base rate ~25% before any evidence
    coefficients={
        "log_liquidity": 0.30,  # deeper pools survive longer
        "log_mcap": -0.28,  # a high entry cap leaves less upside
        "liq_to_mcap": 1.60,  # real depth relative to cap is the best free tell
        "clusters": 0.34,  # independent conviction, already sybil-collapsed
        "log_age_minutes": -0.12,  # very old tokens rarely re-run
        "buy_sell_ratio": 0.22,
        "log_usd_spent": 0.18,  # smart money sizing up means something
        "safety_safe": 0.45,
        "safety_danger": -1.50,
    },
    is_fitted=False,
)


def build_features(
    *,
    liquidity_usd: float | None,
    mcap_usd: float | None,
    distinct_clusters: int,
    age_minutes: int | None,
    buys_24h: int | None,
    sells_24h: int | None,
    usd_spent: float | None,
    safety_verdict: str,
) -> dict[str, float]:
    """Map a signal snapshot onto model features.

    Only snapshot fields are read, so a feature can never leak information
    that was unavailable at signal time.
    """
    liq = max(0.0, liquidity_usd or 0.0)
    mcap = max(1.0, mcap_usd or 0.0)
    ratio = (liq / mcap) if mcap > 0 else 0.0
    buys = buys_24h or 0
    sells = sells_24h or 0
    return {
        "log_liquidity": math.log10(1.0 + liq),
        "log_mcap": math.log10(1.0 + mcap),
        # Clipped: a 1.0+ ratio is a data artefact, not extra safety.
        "liq_to_mcap": min(1.0, ratio),
        "clusters": float(min(distinct_clusters, 10)),
        "log_age_minutes": math.log10(1.0 + max(0, age_minutes or 0)),
        "buy_sell_ratio": min(3.0, buys / sells) if sells > 0 else (1.0 if buys else 0.0),
        "log_usd_spent": math.log10(1.0 + max(0.0, usd_spent or 0.0)),
        "safety_safe": 1.0 if safety_verdict == "safe" else 0.0,
        "safety_danger": 1.0 if safety_verdict == "danger" else 0.0,
    }


def score_from_probability(p2x: float) -> float:
    """A 0-100 display score. Purely a rescaling of the probability."""
    return round(100.0 * max(0.0, min(1.0, p2x)), 1)


def fit(
    samples: list[dict[str, Any]],
    *,
    epochs: int = 400,
    learning_rate: float = 0.25,
    l2: float = 0.01,
) -> QualityModel:
    """Fit logistic regression by gradient descent on resolved signals.

    Each sample needs ``features`` (the dict from ``build_features``) and
    ``reached_2x`` (bool). Falls back to the prior when there is not enough
    data, so the caller never has to special-case a cold start.
    """
    usable = [s for s in samples if s.get("features") and s.get("reached_2x") is not None]
    if len(usable) < MIN_SAMPLES_TO_FIT:
        return PRIOR

    names = sorted(PRIOR.coefficients)
    rows = [[float(s["features"].get(n, 0.0)) for n in names] for s in usable]
    labels = [1.0 if s["reached_2x"] else 0.0 for s in usable]
    n, d = len(rows), len(names)

    means = [sum(r[j] for r in rows) / n for j in range(d)]
    stds = [max(1e-6, (sum((r[j] - means[j]) ** 2 for r in rows) / n) ** 0.5) for j in range(d)]
    scaled = [[(r[j] - means[j]) / stds[j] for j in range(d)] for r in rows]

    weights = [0.0] * d
    bias = math.log(max(1e-6, sum(labels)) / max(1e-6, n - sum(labels)))

    for _ in range(epochs):
        grad_w = [0.0] * d
        grad_b = 0.0
        for row, label in zip(scaled, labels, strict=True):
            z = bias + sum(weights[j] * row[j] for j in range(d))
            error = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))) - label
            grad_b += error
            for j in range(d):
                grad_w[j] += error * row[j]
        bias -= learning_rate * grad_b / n
        for j in range(d):
            weights[j] -= learning_rate * (grad_w[j] / n + l2 * weights[j])

    # Undo standardisation so the model consumes raw features like the prior.
    raw = {names[j]: weights[j] / stds[j] for j in range(d)}
    intercept = bias - sum(weights[j] * means[j] / stds[j] for j in range(d))
    return QualityModel(
        intercept=intercept,
        coefficients=raw,
        is_fitted=True,
        trained_on=n,
        feature_means=dict(zip(names, means, strict=True)),
    )
