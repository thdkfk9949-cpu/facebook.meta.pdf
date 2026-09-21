"""Creative fatigue — high frequency plus a real CTR decline.

Frequency alone is not fatigue. A retargeting ad set at frequency 6 against a
small warm list may be fine. Fatigue is frequency climbing *while* response
falls, so this check requires both, and it puts the CTR drop through a
significance test: impression counts are large, so a genuine decline clears
the test easily and a noisy one does not.
"""

from __future__ import annotations

from ..config import Thresholds
from ..fetch import Snapshot
from ..stats import two_proportion_p
from .base import CheckResult, Confidence, Finding, Severity, register


@register("delivery.fatigue", "Creative fatigue (frequency up, response down)")
def check_fatigue(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult("delivery.fatigue", "Creative fatigue (frequency up, response down)")
    account_spend = snap.account_insights.spend or sum(
        c.insights.spend for c in snap.campaigns
    )
    for adset in snap.adsets:
        if not adset.is_delivering:
            continue
        cur, prev = adset.insights, adset.prev_insights
        if cur.frequency < th.frequency_warn or cur.impressions <= 0:
            continue
        if account_spend > 0 and cur.spend / account_spend < th.min_spend_share_to_report:
            continue

        # CTR here is computed from raw counts, not Meta's ctr field, so the
        # significance test and the reported percentages agree with each other.
        cur_ctr = cur.clicks / cur.impressions
        if prev.impressions <= 0:
            result.findings.append(
                Finding(
                    check_id="delivery.fatigue",
                    severity=Severity.LOW,
                    confidence=Confidence.HEURISTIC,
                    level="adset",
                    entity_id=adset.id,
                    entity_name=adset.name,
                    title=f"Frequency {cur.frequency:.1f} with no prior period to compare",
                    detail=(
                        f"The same people have seen these ads {cur.frequency:.1f} "
                        "times on average, but there is no previous window of "
                        "delivery to measure a response decline against."
                    ),
                    recommendation=(
                        "Re-check after another full window. If frequency keeps "
                        "climbing, add creative before CPA moves."
                    ),
                    evidence={"frequency": round(cur.frequency, 2)},
                    spend_at_stake=cur.spend,
                )
            )
            continue

        prev_ctr = prev.clicks / prev.impressions
        if prev_ctr <= 0:
            continue
        decline_pct = (prev_ctr - cur_ctr) / prev_ctr * 100.0
        if decline_pct < th.ctr_decline_pct:
            continue

        p = two_proportion_p(cur.clicks, cur.impressions, prev.clicks, prev.impressions)
        if p is None:
            result.findings.append(
                Finding(
                    check_id="delivery.fatigue",
                    severity=Severity.INFO,
                    confidence=Confidence.HEURISTIC,
                    level="adset",
                    entity_id=adset.id,
                    entity_name=adset.name,
                    title="Possible fatigue, too few impressions to test",
                    detail=(
                        f"CTR is down {decline_pct:.0f}% at frequency "
                        f"{cur.frequency:.1f}, but impression volume is too low "
                        "for the difference to be tested."
                    ),
                    recommendation="Gather more delivery before acting.",
                    evidence={
                        "frequency": round(cur.frequency, 2),
                        "ctr_decline_pct": round(decline_pct, 1),
                    },
                    spend_at_stake=cur.spend,
                )
            )
            continue
        if p > th.alpha:
            continue  # decline is not distinguishable from noise

        severe = cur.frequency >= th.frequency_high
        result.findings.append(
            Finding(
                check_id="delivery.fatigue",
                severity=Severity.HIGH if severe else Severity.MEDIUM,
                confidence=Confidence.MEASURED,
                level="adset",
                entity_id=adset.id,
                entity_name=adset.name,
                title=f"Fatigued: frequency {cur.frequency:.1f}, CTR down {decline_pct:.0f}%",
                detail=(
                    f"CTR fell from {prev_ctr * 100:.2f}% to {cur_ctr * 100:.2f}% "
                    f"({decline_pct:.0f}% relative) while average frequency reached "
                    f"{cur.frequency:.1f}. The decline is statistically significant "
                    f"(p={p:.4f} over {cur.impressions:,} vs {prev.impressions:,} "
                    "impressions), so this is wear-out, not noise."
                ),
                recommendation=(
                    "Ship new creative — new hook and visual, not a recoloured "
                    "version of the same ad. Raising budget on a fatigued ad set "
                    "buys more impressions of the thing people have stopped "
                    "responding to."
                ),
                evidence={
                    "frequency": round(cur.frequency, 2),
                    "ctr_now_pct": round(cur_ctr * 100, 3),
                    "ctr_prev_pct": round(prev_ctr * 100, 3),
                    "ctr_decline_pct": round(decline_pct, 1),
                    "p_value": round(p, 6),
                    "impressions_now": cur.impressions,
                    "impressions_prev": prev.impressions,
                },
                spend_at_stake=cur.spend,
            )
        )
    return result
