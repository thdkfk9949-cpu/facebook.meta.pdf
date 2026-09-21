"""Conversion tracking and goal coherence.

Misconfigured tracking is the failure that makes every other number in the
account a lie, so these findings outrank efficiency ones: there is no point
optimising toward a CPA that is measured wrong.
"""

from __future__ import annotations

from collections import defaultdict

from ..config import Thresholds
from ..currency import fmt
from ..fetch import NON_CONVERSION_GOALS, Snapshot
from .base import CheckResult, Confidence, Finding, Severity, register

# Objectives whose whole point is a conversion further down the funnel.
SALES_OBJECTIVES = {"OUTCOME_SALES", "CONVERSIONS", "PRODUCT_CATALOG_SALES"}
LEAD_OBJECTIVES = {"OUTCOME_LEADS", "LEAD_GENERATION"}
CONVERSION_OBJECTIVES = SALES_OBJECTIVES | LEAD_OBJECTIVES


@register("tracking.goal_mismatch", "Conversion campaigns buying traffic instead")
def check_goal_mismatch(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult(
        "tracking.goal_mismatch", "Conversion campaigns buying traffic instead"
    )
    cur = snap.currency
    for campaign in snap.campaigns:
        if campaign.objective not in CONVERSION_OBJECTIVES:
            continue
        for adset in campaign.active_adsets:
            if adset.optimization_goal not in NON_CONVERSION_GOALS:
                continue
            result.findings.append(
                Finding(
                    check_id="tracking.goal_mismatch",
                    severity=Severity.CRITICAL,
                    confidence=Confidence.STRUCTURAL,
                    level="adset",
                    entity_id=adset.id,
                    entity_name=adset.name,
                    title=(
                        f"Optimising for {adset.optimization_goal} inside a "
                        f"{campaign.objective} campaign"
                    ),
                    detail=(
                        f"The campaign objective is {campaign.objective}, but this "
                        f"ad set tells Meta to optimise for "
                        f"{adset.optimization_goal}. Meta will faithfully deliver "
                        "the cheapest clicks it can find, which is a different "
                        "population from the people who buy. It has spent "
                        f"{fmt(adset.insights.spend, cur)} in this window doing so."
                    ),
                    recommendation=(
                        "Switch the optimization goal to OFFSITE_CONVERSIONS with "
                        "the right event. If event volume is too low to support "
                        "that, optimise for an earlier event rather than for "
                        "clicks."
                    ),
                    evidence={
                        "campaign_objective": campaign.objective,
                        "optimization_goal": adset.optimization_goal,
                        "billing_event": adset.billing_event,
                    },
                    spend_at_stake=adset.insights.spend,
                )
            )
    return result


@register("tracking.missing_pixel", "Conversion ad sets with no event configured")
def check_missing_pixel(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult(
        "tracking.missing_pixel", "Conversion ad sets with no event configured"
    )
    for adset in snap.adsets:
        if not adset.is_delivering or not adset.optimizes_for_conversions:
            continue
        promoted = adset.promoted_object or {}
        has_target = bool(
            promoted.get("pixel_id")
            or promoted.get("application_id")
            or promoted.get("product_catalog_id")
            or promoted.get("page_id")
            or promoted.get("offline_conversion_data_set_id")
        )
        if has_target and promoted.get("custom_event_type"):
            continue
        result.findings.append(
            Finding(
                check_id="tracking.missing_pixel",
                severity=Severity.CRITICAL,
                confidence=Confidence.STRUCTURAL,
                level="adset",
                entity_id=adset.id,
                entity_name=adset.name,
                title="Conversion optimisation with no event target",
                detail=(
                    f"Optimization goal is {adset.optimization_goal}, but "
                    f"promoted_object is {promoted or '{}'} — there is no pixel "
                    "and event pair for Meta to optimise toward."
                ),
                recommendation=(
                    "Set the conversion location and event on this ad set. Until "
                    "then its delivery is effectively unoptimised."
                ),
                evidence={"promoted_object": promoted, "optimization_goal": adset.optimization_goal},
                spend_at_stake=adset.insights.spend,
            )
        )
    return result


@register("tracking.silent", "Spend with zero recorded conversions")
def check_silent_tracking(snap: Snapshot, th: Thresholds) -> CheckResult:
    """Spend well past the expected CPA with nothing recorded.

    One ad set with no conversions is bad luck. Several, all silent, while the
    account records conversions elsewhere, is a tracking fault.
    """
    result = CheckResult("tracking.silent", "Spend with zero recorded conversions")
    account_conv = 0.0
    account_spend = snap.account_insights.spend
    for adset in snap.adsets:
        account_conv += adset.insights.conversions(adset.conversion_action_types())
    if account_conv <= 0:
        if account_spend > 0:
            result.findings.append(
                Finding(
                    check_id="tracking.silent",
                    severity=Severity.CRITICAL,
                    confidence=Confidence.MEASURED,
                    level="account",
                    entity_id=snap.account_id,
                    entity_name=snap.account_name,
                    title="No conversions recorded anywhere in the window",
                    detail=(
                        f"The account spent {fmt(account_spend, snap.currency)} "
                        f"over {snap.window_days} days and Meta recorded zero "
                        "conversion events. Either the pixel or Conversions API "
                        "is not firing, the event name does not match what the "
                        "ad sets optimise for, or the audit is reading the wrong "
                        "action type."
                    ),
                    recommendation=(
                        "Check the pixel in Events Manager first. If events do "
                        "appear there, re-run with --conversion-action set to the "
                        "action type your account actually reports."
                    ),
                    evidence={"account_spend": round(account_spend, 2)},
                    spend_at_stake=account_spend,
                )
            )
        else:
            result.skipped_reason = "no spend in the window"
        return result

    account_cpa = account_spend / account_conv if account_conv else 0.0
    if account_cpa <= 0:
        return result

    for adset in snap.adsets:
        if not adset.is_delivering or not adset.optimizes_for_conversions:
            continue
        conv = adset.insights.conversions(adset.conversion_action_types())
        if conv > 0:
            continue
        # Three times the account CPA with nothing to show is past bad luck.
        if adset.insights.spend < account_cpa * 3:
            continue
        result.findings.append(
            Finding(
                check_id="tracking.silent",
                severity=Severity.HIGH,
                confidence=Confidence.MEASURED,
                level="adset",
                entity_id=adset.id,
                entity_name=adset.name,
                title=f"{fmt(adset.insights.spend, snap.currency)} spent, zero conversions",
                detail=(
                    f"Spend is {adset.insights.spend / account_cpa:.1f}x the "
                    f"account CPA of {fmt(account_cpa, snap.currency)} with no "
                    "recorded conversion. Either this ad set genuinely does not "
                    "convert, or its event is not being attributed."
                ),
                recommendation=(
                    "Confirm the event fires for this ad set's destination before "
                    "pausing it — pausing a tracking fault hides the fault."
                ),
                evidence={
                    "spend": round(adset.insights.spend, 2),
                    "account_cpa": round(account_cpa, 2),
                    "promoted_object": adset.promoted_object,
                },
                spend_at_stake=adset.insights.spend,
            )
        )
    return result


@register("tracking.attribution_mix", "Inconsistent attribution windows")
def check_attribution_mix(snap: Snapshot, th: Thresholds) -> CheckResult:
    """Ad sets in one campaign measured on different attribution settings.

    Their CPAs are then not comparable, and any "winner" picked between them
    is partly an artefact of the setting.
    """
    result = CheckResult("tracking.attribution_mix", "Inconsistent attribution windows")
    for campaign in snap.campaigns:
        if not campaign.is_delivering:
            continue
        by_spec: dict[str, list[str]] = defaultdict(list)
        for adset in campaign.active_adsets:
            key = repr(sorted((adset.attribution_spec or []), key=repr))
            by_spec[key].append(adset.name or adset.id)
        if len(by_spec) <= 1:
            continue
        result.findings.append(
            Finding(
                check_id="tracking.attribution_mix",
                severity=Severity.MEDIUM,
                confidence=Confidence.STRUCTURAL,
                level="campaign",
                entity_id=campaign.id,
                entity_name=campaign.name,
                title=f"{len(by_spec)} different attribution settings in one campaign",
                detail=(
                    "Ad sets in this campaign use different attribution windows, "
                    "so their cost-per-result figures are measured on different "
                    "rulers and cannot be compared to each other."
                ),
                recommendation=(
                    "Align the attribution setting across the campaign before "
                    "reading any comparison between these ad sets."
                ),
                evidence={"groups": {k[:80]: v for k, v in by_spec.items()}},
                spend_at_stake=campaign.insights.spend,
            )
        )
    return result
