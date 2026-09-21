"""Campaigns split into more ad sets than their budget can support.

The companion to learning.underbudgeted, one level up. Where that check asks
"can this ad set exit learning?", this one asks "how many ad sets could this
campaign's budget actually support?" and states the number. That number is
the consolidation target.
"""

from __future__ import annotations

from ..config import Thresholds
from ..currency import fmt
from ..fetch import Snapshot
from .base import CheckResult, Confidence, Finding, Severity, register
from .learning import required_daily_budget


@register("structure.fragmentation", "Campaigns split across too many ad sets")
def check_fragmentation(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult(
        "structure.fragmentation", "Campaigns split across too many ad sets"
    )
    cur = snap.currency
    for campaign in snap.campaigns:
        if not campaign.is_delivering:
            continue
        active = campaign.active_adsets
        if len(active) < 2:
            continue
        conv_actions = active[0].conversion_action_types()
        conv = campaign.insights.conversions(conv_actions)
        if conv <= 0:
            continue
        cpa = campaign.insights.spend / conv

        daily_budget = campaign.daily_budget
        if not daily_budget:
            daily_budget = sum(a.daily_budget for a in active)
        if daily_budget <= 0:
            continue

        required = required_daily_budget(cpa, th)
        if required <= 0:
            continue
        supportable = int(daily_budget // required)
        if supportable >= len(active):
            continue

        result.findings.append(
            Finding(
                check_id="structure.fragmentation",
                severity=Severity.HIGH if supportable == 0 else Severity.MEDIUM,
                confidence=Confidence.STRUCTURAL,
                level="campaign",
                entity_id=campaign.id,
                entity_name=campaign.name,
                title=(
                    f"{len(active)} active ad sets, budget supports "
                    + (
                        "none at this CPA"
                        if supportable == 0
                        else f"{supportable}"
                    )
                ),
                detail=(
                    f"Campaign spends {fmt(daily_budget, cur)}/day at a CPA of "
                    f"{fmt(cpa, cur)}. Each ad set needs "
                    f"{fmt(required, cur)}/day to reach "
                    f"{th.events_to_exit_learning} events/week, so this budget "
                    f"supports {supportable} such ad set(s) — but it is split "
                    f"across {len(active)}. Every one of them is starved."
                ),
                recommendation=(
                    (
                        f"Consolidate to 1 ad set — and note that even that one "
                        f"needs {fmt(required, cur)}/day, which is above the "
                        f"current {fmt(daily_budget, cur)}/day, so also raise the "
                        f"budget or optimise for an earlier, cheaper event."
                        if supportable == 0
                        else f"Consolidate to {supportable} ad set(s)."
                    )
                    + " If the audiences genuinely must stay separate, raise the "
                    f"campaign budget to {fmt(required * len(active), cur)}/day "
                    "instead — but consolidating is the free option."
                ),
                evidence={
                    "active_adsets": len(active),
                    "daily_budget": round(daily_budget, 2),
                    "cpa": round(cpa, 2),
                    "required_per_adset": round(required, 2),
                    "supportable_adsets": supportable,
                },
                spend_at_stake=campaign.insights.spend,
            )
        )
    return result
