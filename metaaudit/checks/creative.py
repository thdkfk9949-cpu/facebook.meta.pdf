"""Creative supply — how much the algorithm has to choose between.

Meta's delivery system optimises across the ads inside an ad set. Give it two
ads and there is nothing to optimise; the ad set fatigues at the speed of its
single working creative. This is the cheapest lever on the whole account,
which is why it is worth flagging even though it is a heuristic.
"""

from __future__ import annotations

import hashlib

from ..config import Thresholds
from ..fetch import Snapshot
from .base import CheckResult, Confidence, Finding, Severity, register


def _fingerprint(text: str) -> str:
    """Normalise so trivial edits do not read as a distinct creative."""
    normalized = " ".join((text or "").lower().split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest() if normalized else ""


@register("creative.supply", "Ad sets with too few distinct creatives")
def check_creative_supply(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult("creative.supply", "Ad sets with too few distinct creatives")
    account_spend = snap.account_insights.spend or sum(
        c.insights.spend for c in snap.campaigns
    )
    for adset in snap.adsets:
        if not adset.is_delivering:
            continue
        if adset.is_dynamic_creative:
            continue  # the asset feed supplies the variation
        live = [ad for ad in adset.ads if ad.is_delivering]
        if not live:
            continue
        if account_spend > 0 and (
            adset.insights.spend / account_spend < th.min_spend_share_to_report
        ):
            continue

        texts = {_fingerprint(ad.primary_text()) for ad in live}
        texts.discard("")
        distinct = len(texts)

        if len(live) < th.min_active_ads_per_adset:
            result.findings.append(
                Finding(
                    check_id="creative.supply",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.HEURISTIC,
                    level="adset",
                    entity_id=adset.id,
                    entity_name=adset.name,
                    title=f"Only {len(live)} active ad(s)",
                    detail=(
                        f"This ad set is delivering {len(live)} ad(s). Meta's "
                        "delivery system optimises across the ads in an ad set; "
                        "with this few there is little to optimise, and fatigue "
                        "arrives as soon as the one working ad wears out."
                    ),
                    recommendation=(
                        f"Run at least {th.min_active_ads_per_adset}-5 genuinely "
                        "different ads per ad set — different hook, format and "
                        "visual, not colour variants."
                    ),
                    evidence={"active_ads": len(live), "distinct_primary_texts": distinct},
                    spend_at_stake=adset.insights.spend,
                )
            )
        elif distinct and distinct / len(live) < th.min_distinct_creative_ratio:
            result.findings.append(
                Finding(
                    check_id="creative.supply",
                    severity=Severity.LOW,
                    confidence=Confidence.HEURISTIC,
                    level="adset",
                    entity_id=adset.id,
                    entity_name=adset.name,
                    title=f"{len(live)} ads but only {distinct} distinct primary texts",
                    detail=(
                        "The ads in this ad set are near-duplicates of each "
                        "other, so the apparent creative volume is not real "
                        "variety for the algorithm to test."
                    ),
                    recommendation=(
                        "Replace the duplicates with genuinely different angles: "
                        "problem-first, social proof, offer-led, objection-handling."
                    ),
                    evidence={"active_ads": len(live), "distinct_primary_texts": distinct},
                    spend_at_stake=adset.insights.spend,
                )
            )
    return result
