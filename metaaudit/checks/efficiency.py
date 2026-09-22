"""Performance outliers — but only the ones the sample can actually support.

Every comparison here is between an ad set and the pooled rest of its own
campaign, because that holds objective, audience strategy and attribution
roughly constant. An ad set is only named as an underperformer when the
difference clears a two-proportion test. Everything that fails the test is
reported as "not enough data" instead, which is the finding that saves money
most often: it tells the operator to stop making decisions they cannot yet
justify.
"""

from __future__ import annotations

from ..config import Thresholds
from ..currency import fmt
from ..fetch import Snapshot
from ..stats import assess, cpa_interval, cpa_relative_margin, two_proportion_p
from .base import CheckResult, Confidence, Finding, Severity, register


@register("efficiency.outliers", "Ad sets measurably worse than their campaign")
def check_outliers(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult(
        "efficiency.outliers", "Ad sets measurably worse than their campaign"
    )
    cur = snap.currency
    for campaign in snap.campaigns:
        active = [a for a in campaign.active_adsets if a.optimizes_for_conversions]
        if len(active) < 2:
            continue
        actions = active[0].conversion_action_types()

        for adset in active:
            mine_conv = int(adset.insights.conversions(actions))
            mine_clicks = adset.insights.clicks
            others = [a for a in active if a.id != adset.id]
            rest_conv = int(sum(a.insights.conversions(actions) for a in others))
            rest_clicks = sum(a.insights.clicks for a in others)
            if mine_clicks <= 0 or rest_clicks <= 0:
                continue

            p = two_proportion_p(mine_conv, mine_clicks, rest_conv, rest_clicks)
            mine_cvr = mine_conv / mine_clicks
            rest_cvr = rest_conv / rest_clicks
            if p is None or p > th.alpha or mine_cvr >= rest_cvr:
                continue

            sufficiency = assess(mine_conv, th.min_conversions_for_claim)
            mine_cpa = adset.insights.spend / mine_conv if mine_conv else 0.0
            margin = cpa_relative_margin(mine_conv)
            margin_note = (
                f" Its CPA estimate carries roughly +/-{margin * 100:.0f}% at 95% "
                f"confidence on {mine_conv} conversions."
                if margin
                else ""
            )
            result.findings.append(
                Finding(
                    check_id="efficiency.outliers",
                    severity=Severity.MEDIUM if sufficiency.ok else Severity.LOW,
                    confidence=(
                        Confidence.MEASURED if sufficiency.ok else Confidence.HEURISTIC
                    ),
                    level="adset",
                    entity_id=adset.id,
                    entity_name=adset.name,
                    title=(
                        f"Converts at {mine_cvr * 100:.2f}% vs "
                        f"{rest_cvr * 100:.2f}% for the rest of the campaign"
                    ),
                    detail=(
                        f"Conversion rate from clicks is significantly below its "
                        f"campaign siblings (p={p:.4f}). Spend "
                        f"{fmt(adset.insights.spend, cur)}, "
                        f"{mine_conv} conversions"
                        + (f", CPA {fmt(mine_cpa, cur)}." if mine_conv else ".")
                        + margin_note
                        + (
                            ""
                            if sufficiency.ok
                            else f" Treat as provisional: {sufficiency.reason}."
                        )
                    ),
                    recommendation=(
                        "Before pausing, check it is not a tracking or landing "
                        "page problem — and check the learning findings first: an "
                        "ad set starved below 50 events/week will look like a "
                        "loser whether or not it is one."
                    ),
                    evidence={
                        "cvr_pct": round(mine_cvr * 100, 3),
                        "campaign_rest_cvr_pct": round(rest_cvr * 100, 3),
                        "p_value": round(p, 6),
                        "conversions": mine_conv,
                        "clicks": mine_clicks,
                        "sample_sufficient": sufficiency.ok,
                    },
                    spend_at_stake=adset.insights.spend,
                )
            )
    return result


@register("efficiency.undecidable", "Ad sets that cannot yet be judged")
def check_undecidable(snap: Snapshot, th: Thresholds) -> CheckResult:
    """Spending real money while producing no decision-grade signal.

    Not a performance verdict — the opposite. These are the ad sets where
    acting on the reported CPA would be acting on noise.
    """
    result = CheckResult("efficiency.undecidable", "Ad sets that cannot yet be judged")
    account_spend = snap.account_insights.spend or sum(
        c.insights.spend for c in snap.campaigns
    )
    for adset in snap.adsets:
        if not adset.is_delivering or not adset.optimizes_for_conversions:
            continue
        if account_spend > 0 and (
            adset.insights.spend / account_spend < th.min_spend_share_to_report
        ):
            continue
        conv = int(adset.insights.conversions(adset.conversion_action_types()))
        sufficiency = assess(conv, th.min_conversions_for_claim)
        if sufficiency.ok or conv == 0:
            continue  # zero-conversion ad sets are handled by tracking.silent
        cpa = adset.insights.spend / conv
        # Exact Poisson bounds, not cpa*(1 +/- margin): below four conversions
        # the symmetric form puts the lower bound under zero, and a CPA that
        # reads "between -78,934 and 243,380" teaches the reader nothing except
        # to distrust the tool.
        bounds = cpa_interval(adset.insights.spend, conv)
        low, high = bounds if bounds else (cpa, cpa)
        margin = cpa_relative_margin(conv)
        high_text = (
            "no upper bound at all" if high == float("inf")
            else fmt(high, snap.currency)
        )
        result.findings.append(
            Finding(
                check_id="efficiency.undecidable",
                severity=Severity.INFO,
                confidence=Confidence.MEASURED,
                level="adset",
                entity_id=adset.id,
                entity_name=adset.name,
                title=f"CPA is {fmt(cpa, snap.currency)} +/- {(margin or 0) * 100:.0f}%",
                detail=(
                    f"{conv} conversions over {snap.window_days} days puts the "
                    f"true CPA somewhere between {fmt(low, snap.currency)} and "
                    f"{high_text} (exact Poisson, 95%). Any decision that would "
                    "flip inside that range is a coin toss."
                ),
                recommendation=(
                    f"Let it reach {th.min_conversions_for_claim} conversions "
                    "before judging it, or accept that you are guessing."
                ),
                evidence={
                    "conversions": conv,
                    "cpa": round(cpa, 2),
                    "cpa_low": round(low, 2),
                    "cpa_high": None if high == float("inf") else round(high, 2),
                    "interval_method": "poisson_exact_95",
                },
                spend_at_stake=adset.insights.spend,
            )
        )
    return result
