"""Where the funnel actually breaks.

Every other check in this tool asks whether Meta is set up correctly. This
one asks a question Meta's own reporting buries: of the people the ads
deliver, where do they stop?

It names the transition that loses the most people, and reports its rate with
a Wilson interval rather than a bare percentage — because the useful output is
not "1.4% converted" but "fewer than 7.5% of them converted, with 95%
confidence". The first is a number off one observation; the second is a bound
that can carry a decision.

There is no benchmark here on purpose. Whether a 30% drop from click to
landing page view is bad depends on the business, and a tool that asserts
otherwise is guessing. What does not depend on the business is *which step*
loses the most people, and that is what this reports.
"""

from __future__ import annotations

from ..config import Thresholds
from ..currency import fmt
from ..fetch import Campaign, Snapshot
from ..funnel import price_funnel
from ..stats import wilson_interval
from .base import CheckResult, Confidence, Finding, Severity, register

# What a collapse at each transition usually means. Keyed by (from, to), and
# deliberately about causes outside Meta — by this point in the funnel the ad
# has already done its job.
DIAGNOSIS = {
    ("LINK_CLICKS", "LANDING_PAGE_VIEWS"): (
        "People clicked and left before the page rendered. This is almost "
        "always load time, a redirect chain, or a broken link — not targeting "
        "and not creative."
    ),
    ("LANDING_PAGE_VIEWS", "VIEW_CONTENT"): (
        "The page loaded but the ViewContent event did not fire for most "
        "visitors. Either the pixel is missing on that template, or the page "
        "is not the product page the ad promised."
    ),
    ("VIEW_CONTENT", "ADD_TO_CART"): (
        "People saw the product and did not want it at this price, in this "
        "framing. No change to the ad account fixes this."
    ),
    ("VIEW_CONTENT", "INITIATE_CHECKOUT"): (
        "People saw the offer and did not act on it. The ads are delivering "
        "interested traffic; the page or the offer is not converting it. No "
        "change to the ad account fixes this."
    ),
    ("ADD_TO_CART", "INITIATE_CHECKOUT"): (
        "Carts are filling and not moving. Look at shipping cost disclosure "
        "and whether checkout requires an account."
    ),
    ("INITIATE_CHECKOUT", "PURCHASE"): (
        "Checkout is losing people mid-payment. Look at payment methods, "
        "unexpected fees at the last step, and mobile form friction."
    ),
    ("ADD_PAYMENT_INFO", "PURCHASE"): (
        "Payment details entered and the order not placed — usually a failing "
        "payment processor or a final-step error."
    ),
}


def _transitions(campaign: Campaign, th: Thresholds):
    """Consecutive funnel pairs with enough upstream volume to bound a rate."""
    steps = price_funnel(
        campaign.insights,
        events_to_exit_learning=th.events_to_exit_learning,
        min_events_to_price=th.min_conversions_for_claim,
    )
    for upstream, downstream in zip(steps, steps[1:]):
        if upstream.count < th.funnel_min_upstream:
            continue
        yield upstream, downstream


@register("funnel.dropoff", "Where the funnel loses the most people")
def check_funnel_dropoff(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult("funnel.dropoff", "Where the funnel loses the most people")
    cur = snap.currency
    evaluated = 0

    for campaign in snap.campaigns:
        if not campaign.is_delivering:
            continue
        pairs = list(_transitions(campaign, th))
        if not pairs:
            continue
        evaluated += 1

        # The transition that loses the most people, not the lowest rate: a
        # step that drops 90% of three people is noise, one that drops 70% of
        # a hundred is where the money goes.
        upstream, downstream = max(pairs, key=lambda p: p[0].count - p[1].count)
        kept = downstream.count
        total = upstream.count
        rate = kept / total
        lo, hi = wilson_interval(kept, total)
        lost = total - kept

        if rate >= th.funnel_collapse_ratio:
            continue

        severity = Severity.HIGH if rate < 0.2 else Severity.MEDIUM
        diagnosis = DIAGNOSIS.get(
            (upstream.event, downstream.event),
            "This is the narrowest point in the funnel.",
        )
        result.findings.append(
            Finding(
                check_id="funnel.dropoff",
                # It rests on this account's numbers, and the interval is what
                # makes it safe to act on.
                confidence=Confidence.MEASURED,
                severity=severity,
                level="campaign",
                entity_id=campaign.id,
                entity_name=campaign.name,
                title=(
                    f"{lost} of {total} lost between {upstream.label} and "
                    f"{downstream.label}"
                ),
                detail=(
                    f"{total} reached {upstream.label} and {kept} reached "
                    f"{downstream.label} — {rate * 100:.1f}%, and with 95% "
                    f"confidence no better than {hi * 100:.1f}%. "
                    f"{fmt(campaign.insights.spend, cur)} of spend passes "
                    f"through this step. {diagnosis}"
                ),
                recommendation=(
                    f"Fix this step before changing budgets, audiences or "
                    f"creative. Everything upstream of it is already working: "
                    f"{total} people got this far. Optimising delivery harder "
                    f"only sends more people into the same wall."
                ),
                evidence={
                    "from_event": upstream.event,
                    "to_event": downstream.event,
                    "upstream_count": total,
                    "downstream_count": kept,
                    "rate": round(rate, 4),
                    "rate_ci_low": round(lo, 4),
                    "rate_ci_high": round(hi, 4),
                    "cost_per_upstream_event": round(upstream.cost, 2),
                    "cost_per_downstream_event": round(downstream.cost, 2),
                },
                spend_at_stake=campaign.insights.spend,
            )
        )

    if not evaluated and not result.findings:
        result.skipped_reason = (
            f"no campaign has {th.funnel_min_upstream} events at any funnel "
            f"step, so no drop-off rate can be bounded — the account has not "
            f"yet delivered enough traffic to locate a bottleneck"
        )
    return result
