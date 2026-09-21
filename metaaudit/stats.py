"""Statistical guardrails.

The single most expensive habit in paid social is acting on differences that
are not there: pausing an ad set after three bad days, declaring a winner off
eleven conversions. Every performance claim this tool makes has to clear a
test defined here, or it is reported as "not enough data" instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _norm_sf(z: float) -> float:
    """Upper-tail probability of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_proportion_p(
    conv_a: int, trials_a: int, conv_b: int, trials_b: int
) -> float | None:
    """Two-sided p-value for a difference in rates (pooled z-test).

    Returns ``None`` when the normal approximation does not apply — fewer than
    5 expected successes or failures in either arm. Returning None rather than
    a number is the point: the caller must then say "unknown", not "no
    difference".
    """
    if trials_a <= 0 or trials_b <= 0:
        return None
    p_pool = (conv_a + conv_b) / (trials_a + trials_b)
    if p_pool <= 0 or p_pool >= 1:
        return None
    for trials in (trials_a, trials_b):
        if trials * p_pool < 5 or trials * (1 - p_pool) < 5:
            return None
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / trials_a + 1 / trials_b))
    if se == 0:
        return None
    z = (conv_a / trials_a - conv_b / trials_b) / se
    return 2.0 * _norm_sf(abs(z))


def wilson_interval(
    successes: int, trials: int, z: float = 1.96
) -> tuple[float, float]:
    """Wilson score interval — behaves sanely at small n, unlike normal CI."""
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    margin = (
        z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass
class Sufficiency:
    """Whether an entity's data can support a claim at all."""

    conversions: int
    required: int
    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def assess(conversions: int, required: int) -> Sufficiency:
    if conversions >= required:
        return Sufficiency(conversions, required, True, "")
    return Sufficiency(
        conversions,
        required,
        False,
        (
            f"{conversions} conversions in the window (need {required} before a "
            f"performance judgment means anything)"
        ),
    )


def cpa_relative_margin(conversions: int) -> float | None:
    """Rough relative half-width of the CPA estimate.

    Conversion counts are approximately Poisson, so the relative standard
    error of a CPA computed from ``n`` conversions is about ``1/sqrt(n)``.
    At n=10 that is a +/-62% band at 95% — which is why a "CPA" off ten
    conversions tells you almost nothing.
    """
    if conversions <= 0:
        return None
    return 1.96 / math.sqrt(conversions)
