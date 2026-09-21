"""The learning phase, and the arithmetic that decides whether it can ever end.

This is the check that usually finds the money. An ad set needs roughly 50
optimization events per 7 days to leave the learning phase. That requirement
turns into a hard budget floor: at a 40-unit CPA you need 50 x 40 / 7 = ~286
per day. An ad set budgeted at 50 per day against that CPA is not
"underperforming" — it is arithmetically incapable of stabilising, and no
amount of creative or targeting work will change that. The fix is to
consolidate budget into fewer ad sets, which costs nothing.
"""

from __future__ import annotations

from ..config import Thresholds
from ..fetch import Snapshot, AdSet, Campaign
from ..currency import fmt
from .base import CheckResult, Confidence, Finding, Severity, register


def _cpa_for(adset: AdSet, campaign: Campaign | None, snap: Snapshot) -> tuple[float, str]:
    """Best available CPA estimate, and where it came from.

    Falls back up the hierarchy because an ad set with zero conversions has no
    CPA of its own — but the account's CPA still tells us roughly what an
    event costs here.
    """
    actions = adset.conversion_action_types()
    conv = adset.insights.conversions(actions)
    if conv > 0:
        return adset.insights.spend / conv, "own"
    if campaign is not None:
        conv = campaign.insights.conversions(actions)
        if conv > 0:
            return campaign.insights.spend / conv, "campaign"
    conv = snap.account_insights.conversions(actions)
    if conv > 0:
        return snap.account_insights.spend / conv, "account"
    return 0.0, "none"


def required_daily_budget(cpa: float, thresholds: Thresholds) -> float:
    """Daily spend needed to buy 50 optimization events in 7 days."""
    return cpa * thresholds.events_to_exit_learning / 7.0


@register("learning.underbudgeted", "Ad sets that cannot reach 50 events/week")
def check_underbudgeted(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult("learning.underbudgeted", "Ad sets that cannot reach 50 events/week")
    cur = snap.currency
    candidates = [a for a in snap.adsets if a.is_delivering and a.optimizes_for_conversions]
    if not candidates:
        result.skipped_reason = (
            "no delivering ad sets optimize for a conversion event, so the "
            "50-events/week rule does not apply to this account"
        )
        return result

    no_cpa = 0
    for adset in candidates:
        campaign = snap.campaign_of(adset)
        if campaign is None:
            continue
        cpa, source = _cpa_for(adset, campaign, snap)
        if cpa <= 0:
            no_cpa += 1
            continue
        budget = adset.effective_daily_budget(campaign)
        if budget <= 0:
            continue  # lifetime-budget ad set; no daily figure to compare
        required = required_daily_budget(cpa, th)
        if budget >= required * th.underbudget_ratio:
            continue

        achievable_weekly = budget * 7.0 / cpa
        severity = Severity.HIGH if achievable_weekly < 25 else Severity.MEDIUM
        basis = {
            "own": "its own CPA",
            "campaign": "the parent campaign's CPA (this ad set has no conversions yet)",
            "account": "the account CPA (neither this ad set nor its campaign has conversions yet)",
        }[source]
        result.findings.append(
            Finding(
                check_id="learning.underbudgeted",
                severity=severity,
                # Arithmetic, not a performance judgment: no sample needed to
                # know that budget/CPA is below 50 events.
                confidence=Confidence.STRUCTURAL,
                level="adset",
                entity_id=adset.id,
                entity_name=adset.name,
                title=f"Budget supports only ~{achievable_weekly:.0f} events/week",
                detail=(
                    f"Daily budget {fmt(budget, cur)} against a CPA of "
                    f"{fmt(cpa, cur)} (from {basis}) buys about "
                    f"{achievable_weekly:.0f} optimization events per week. "
                    f"Leaving the learning phase needs {th.events_to_exit_learning}. "
                    f"That requires {fmt(required, cur)}/day at this CPA."
                ),
                recommendation=(
                    f"Consolidate. Either raise this ad set to {fmt(required, cur)}/day "
                    f"or merge it into a sibling ad set — the budget is better spent "
                    f"as one ad set that exits learning than as two that never do."
                ),
                evidence={
                    "daily_budget": round(budget, 2),
                    "cpa": round(cpa, 2),
                    "cpa_source": source,
                    "required_daily_budget": round(required, 2),
                    "achievable_weekly_events": round(achievable_weekly, 1),
                    "learning_stage": adset.learning_status,
                },
                spend_at_stake=adset.insights.spend,
            )
        )

    if no_cpa and not result.findings:
        result.skipped_reason = (
            f"{no_cpa} ad set(s) have no conversion data at any level, so no CPA "
            "could be estimated. Re-run with a longer --window, or check that "
            "conversion tracking fires at all (see tracking.* findings)."
        )
    return result


@register("learning.stuck", "Ad sets stuck in or failing the learning phase")
def check_stuck(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult("learning.stuck", "Ad sets stuck in or failing the learning phase")
    reported = False
    for adset in snap.adsets:
        if not adset.is_delivering:
            continue
        info = adset.learning_stage_info or {}
        if not info:
            continue
        reported = True
        status = adset.learning_status
        if status == "FAIL":
            result.findings.append(
                Finding(
                    check_id="learning.stuck",
                    severity=Severity.HIGH,
                    # Meta itself is reporting this state; no inference.
                    confidence=Confidence.MEASURED,
                    level="adset",
                    entity_id=adset.id,
                    entity_name=adset.name,
                    title="Meta reports this ad set as learning limited",
                    detail=(
                        "Meta has marked this ad set LEARNING_LIMITED: it is not "
                        "getting enough weekly events to optimise, so delivery "
                        "and cost stay unstable. This is Meta's own verdict, not "
                        "an inference from the numbers."
                    ),
                    recommendation=(
                        "Merge it with similar ad sets, broaden the audience, or "
                        "move the conversion event earlier in the funnel (e.g. "
                        "optimise for Add to Cart instead of Purchase) so events "
                        "accumulate faster."
                    ),
                    evidence={"learning_stage_info": info},
                    spend_at_stake=adset.insights.spend,
                )
            )
        elif status == "LEARNING":
            result.findings.append(
                Finding(
                    check_id="learning.stuck",
                    severity=Severity.LOW,
                    confidence=Confidence.MEASURED,
                    level="adset",
                    entity_id=adset.id,
                    entity_name=adset.name,
                    title="Still in the learning phase",
                    detail=(
                        "Performance figures for this ad set are not yet "
                        "decision-grade. Cost per result during learning is "
                        "routinely worse than it settles at."
                    ),
                    recommendation=(
                        "Leave it alone. Editing budget, targeting, creative or "
                        "the optimization goal restarts learning and throws away "
                        "the events already gathered."
                    ),
                    evidence={"learning_stage_info": info},
                    spend_at_stake=adset.insights.spend,
                )
            )
    if not reported:
        result.skipped_reason = (
            "no ad set returned learning_stage_info — the field is only "
            "populated while an ad set is delivering"
        )
    return result
