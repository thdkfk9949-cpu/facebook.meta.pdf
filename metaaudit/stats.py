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

    Use :func:`cpa_interval` for the actual bounds. This symmetric margin is
    a readable summary and nothing more: below about 4 conversions it exceeds
    100%, and a CPA cannot be negative.
    """
    if conversions <= 0:
        return None
    return 1.96 / math.sqrt(conversions)


def _gammainc_lower_reg(a: float, x: float) -> float:
    """Regularised lower incomplete gamma P(a, x), to ~1e-12.

    Series below the crossover, continued fraction above it — the standard
    split, because each form loses accuracy in the other's range.
    """
    if x <= 0:
        return 0.0
    if x < a + 1.0:
        term = 1.0 / a
        total = term
        n = a
        for _ in range(1000):
            n += 1.0
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    # Lentz's algorithm for the continued fraction of Q(a, x).
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    return 1.0 - q


def _gamma_ppf(p: float, shape: float) -> float:
    """Quantile of Gamma(shape, 1), by bisection on the CDF."""
    if p <= 0.0:
        return 0.0
    lo, hi = 0.0, max(1.0, shape)
    while _gammainc_lower_reg(shape, hi) < p:
        hi *= 2.0
        if hi > 1e12:
            return hi
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _gammainc_lower_reg(shape, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def poisson_interval(count: int, conf: float = 0.95) -> tuple[float, float]:
    """Exact (Garwood) confidence interval for a Poisson count.

    The normal approximation puts the lower bound below zero for small
    counts, which for a rate — or a CPA derived from one — is not a wide
    estimate but an impossible one. This never does that, and it stays
    correct at n=1, where most of these accounts actually live.
    """
    if count < 0:
        raise ValueError("count must be >= 0")
    alpha = 1.0 - conf
    lower = 0.0 if count == 0 else _gamma_ppf(alpha / 2.0, float(count))
    upper = _gamma_ppf(1.0 - alpha / 2.0, float(count) + 1.0)
    return (lower, upper)


def cpa_interval(
    spend: float, conversions: int, conf: float = 0.95
) -> tuple[float, float] | None:
    """Confidence interval for CPA = spend / conversions.

    Derived by inverting the interval on the conversion count, so the bounds
    are always positive and always ordered. With zero conversions there is no
    upper bound on CPA at all; the caller gets ``None`` and should say so
    rather than print a number.
    """
    if conversions <= 0 or spend <= 0:
        return None
    lower_count, upper_count = poisson_interval(conversions, conf)
    if lower_count <= 0:
        return (spend / upper_count, float("inf"))
    return (spend / upper_count, spend / lower_count)
