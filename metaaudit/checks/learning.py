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
from ..fetch import NON_CONVERSION_GOALS
from ..funnel import cheapest_reachable, nearest_priceable
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


# Kept in step with tracking.goal_mismatch: the objectives where buying
# clicks instead of conversions is a defect rather than a choice.
CONVERSION_OBJECTIVES = {
    "OUTCOME_SALES", "CONVERSIONS", "PRODUCT_CATALOG_SALES",
    "OUTCOME_LEADS", "LEAD_GENERATION",
}


def _funnel_source(adset, campaign):
    """Price the funnel off whichever numbers are thick enough to mean anything.

    An ad set that has just started carries too few actions to price; its
    campaign has the same funnel and more of it.
    """
    if adset.insights.spend >= campaign.insights.spend:
        return adset.insights
    return campaign.insights


def _funnel_evidence(adset, campaign, budget, th) -> dict:
    insights = _funnel_source(adset, campaign)
    reachable = cheapest_reachable(
        insights,
        budget,
        events_to_exit_learning=th.events_to_exit_learning,
        min_events_to_price=th.min_conversions_for_claim,
    )
    nearest = nearest_priceable(
        insights,
        events_to_exit_learning=th.events_to_exit_learning,
        min_events_to_price=th.min_conversions_for_claim,
    )
    out: dict = {}
    if reachable:
        out["reachable_event"] = {
            "event": reachable.event,
            "observed": reachable.count,
            "cost_per_event": round(reachable.cost, 2),
            "required_daily_budget": round(reachable.required_daily, 2),
        }
    if nearest:
        out["deepest_priceable_event"] = {
            "event": nearest.event,
            "observed": nearest.count,
            "cost_per_event": round(nearest.cost, 2),
            "required_daily_budget": round(nearest.required_daily, 2),
        }
    return out


def _recommend(adset, campaign, snap, budget, required, th) -> str:
    """What to actually do — checked against this account, not a template.

    Telling a one-ad-set account to merge into a sibling is advice it cannot
    take, and "raise to 587,307/day" on a 10,000/day account is a number
    nobody will act on. Both were being printed.
    """
    cur = snap.currency
    siblings = [a for a in campaign.active_adsets if a.id != adset.id]
    insights = _funnel_source(adset, campaign)
    reachable = cheapest_reachable(
        insights,
        budget,
        events_to_exit_learning=th.events_to_exit_learning,
        min_events_to_price=th.min_conversions_for_claim,
    )
    nearest = nearest_priceable(
        insights,
        events_to_exit_learning=th.events_to_exit_learning,
        min_events_to_price=th.min_conversions_for_claim,
    )

    parts: list[str] = []
    # A conversion campaign told to buy clicks is what tracking.goal_mismatch
    # calls CRITICAL. Recommending it here to escape the learning phase would
    # have this tool contradict itself, so a shallow event is only ever
    # offered with the trade-off stated and the conversion event priced.
    conversion_objective = campaign.objective in CONVERSION_OBJECTIVES
    shallow = reachable is not None and reachable.event in NON_CONVERSION_GOALS

    if reachable is not None and not (conversion_objective and shallow):
        parts.append(
            f"Optimise for {reachable.event} instead. It costs "
            f"{fmt(reachable.cost, cur)} here ({reachable.count} observed), so "
            f"{th.events_to_exit_learning} a week needs "
            f"{fmt(reachable.required_daily, cur)}/day — inside the "
            f"{fmt(budget, cur)}/day this ad set already has. An event this "
            f"budget can actually buy beats a deeper one it cannot."
        )
        if nearest is not None and nearest.event != reachable.event:
            parts.append(
                f"{nearest.event} would be the better proxy at "
                f"{fmt(nearest.required_daily, cur)}/day if the budget can go "
                f"there."
            )
    elif reachable is not None and shallow and conversion_objective:
        deeper = nearest if nearest and nearest.event != reachable.event else None
        parts.append(
            f"The only event this {fmt(budget, cur)}/day can buy 50 of a week "
            f"is {reachable.event} at {fmt(reachable.required_daily, cur)}/day "
            f"— but this is a {campaign.objective} campaign, and optimising "
            f"for clicks inside one buys the cheapest clicks, not buyers."
        )
        if deeper is not None:
            increase = deeper.required_daily - budget
            parts.append(
                f"Raise the budget to {fmt(deeper.required_daily, cur)}/day "
                f"(+{fmt(increase, cur)}) and optimise for {deeper.event} "
                f"instead: {fmt(deeper.cost, cur)} each over "
                f"{deeper.count} observed, and it is a real step toward the "
                f"purchase rather than away from it."
            )
        else:
            parts.append(
                "Raise the budget until a conversion event is affordable, or "
                "accept that this campaign is buying traffic, not conversions."
            )
    elif nearest is not None:
        parts.append(
            f"No funnel event is reachable at {fmt(budget, cur)}/day. The "
            f"closest is {nearest.event} at {fmt(nearest.cost, cur)} each "
            f"({nearest.count} observed), needing "
            f"{fmt(nearest.required_daily, cur)}/day — aim at that number "
            f"rather than at {fmt(required, cur)}/day."
        )
    else:
        parts.append(
            f"Raise this ad set to {fmt(required, cur)}/day, or optimise for "
            f"an earlier funnel event. There is not yet enough action data "
            f"here to price which earlier event would work."
        )

    if siblings:
        parts.append(
            f"Consolidating also works and costs nothing: this campaign has "
            f"{len(siblings)} other delivering ad set(s), and the budget is "
            f"better spent as one ad set that exits learning than as "
            f"{len(siblings) + 1} that never do."
        )
    return " ".join(parts)


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
                recommendation=_recommend(
                    adset, campaign, snap, budget, required, th
                ),
                evidence={
                    "daily_budget": round(budget, 2),
                    "cpa": round(cpa, 2),
                    "cpa_source": source,
                    "required_daily_budget": round(required, 2),
                    "achievable_weekly_events": round(achievable_weekly, 1),
                    "learning_stage": adset.learning_status,
                    **_funnel_evidence(adset, campaign, budget, th),
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
