"""The standard conversion funnel, and what each step costs to optimise for.

When an ad set's budget cannot buy 50 purchases a week, "raise the budget to
what 50 purchases cost" is arithmetically correct and usually useless — the
number lands far outside what the account will ever spend. The useful question
is the other one: *which event, further up the funnel, is 50-a-week reachable
at the budget that already exists?*

The account's own action counts answer that. They are measured numbers, so a
step is only offered once enough of them have been observed to price it; below
that it is reported as too thin to price, never guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .fetch import Insights

# Funnel order, shallowest first. Each step lists the action types that count
# it, most specific first — the pixel-scoped name wins, because the bare alias
# double-counts when a pixel and an app event both fire.
FUNNEL_STEPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "LINK_CLICKS",
        "link click",
        ("link_click",),
    ),
    (
        "LANDING_PAGE_VIEWS",
        "landing page view",
        ("landing_page_view", "omni_landing_page_view"),
    ),
    (
        "VIEW_CONTENT",
        "content view",
        (
            "offsite_conversion.fb_pixel_view_content",
            "omni_view_content",
            "view_content",
        ),
    ),
    (
        "ADD_TO_CART",
        "add to cart",
        (
            "offsite_conversion.fb_pixel_add_to_cart",
            "omni_add_to_cart",
            "add_to_cart",
        ),
    ),
    (
        "INITIATE_CHECKOUT",
        "checkout start",
        (
            "offsite_conversion.fb_pixel_initiate_checkout",
            "omni_initiated_checkout",
            "initiate_checkout",
        ),
    ),
    (
        "ADD_PAYMENT_INFO",
        "payment info added",
        (
            "offsite_conversion.fb_pixel_add_payment_info",
            "omni_add_payment_info",
            "add_payment_info",
        ),
    ),
    (
        "PURCHASE",
        "purchase",
        (
            "offsite_conversion.fb_pixel_purchase",
            "omni_purchase",
            "purchase",
        ),
    ),
)


@dataclass
class Step:
    """One funnel stage, priced from this entity's own numbers."""

    event: str            # the optimization event / custom_event_type name
    label: str            # how to say it in a sentence
    count: int
    cost: float           # spend per event, major units
    required_daily: float # spend/day to reach the weekly event target
    priceable: bool       # enough observations to quote a cost at all

    @property
    def reachable_at(self) -> float:
        return self.required_daily


def _count(insights: Insights, action_types: Sequence[str]) -> int:
    """First matching action type wins — summing them double-counts."""
    for action_type in action_types:
        if action_type in insights.actions:
            return int(insights.actions[action_type])
    return 0


def price_funnel(
    insights: Insights,
    *,
    events_to_exit_learning: int = 50,
    min_events_to_price: int = 30,
) -> list[Step]:
    """Price every funnel step this entity has data for, shallowest first."""
    spend = insights.spend
    steps: list[Step] = []
    for event, label, action_types in FUNNEL_STEPS:
        count = _count(insights, action_types)
        if count <= 0 or spend <= 0:
            continue
        cost = spend / count
        steps.append(
            Step(
                event=event,
                label=label,
                count=count,
                cost=cost,
                required_daily=cost * events_to_exit_learning / 7.0,
                priceable=count >= min_events_to_price,
            )
        )
    return steps


def cheapest_reachable(
    insights: Insights,
    daily_budget: float,
    *,
    events_to_exit_learning: int = 50,
    min_events_to_price: int = 30,
) -> Step | None:
    """The deepest funnel step that 50-a-week is affordable at this budget.

    Deepest, not cheapest: an event closer to the purchase is a better proxy
    for it, so among the affordable steps the one furthest down wins. Returns
    None when no step is both affordable and backed by enough observations to
    price — in which case the honest answer is that the budget cannot support
    optimisation at any funnel stage, not that some stage might do.
    """
    affordable = [
        step
        for step in price_funnel(
            insights,
            events_to_exit_learning=events_to_exit_learning,
            min_events_to_price=min_events_to_price,
        )
        if step.priceable and step.required_daily <= daily_budget
    ]
    return affordable[-1] if affordable else None


def nearest_priceable(
    insights: Insights,
    *,
    events_to_exit_learning: int = 50,
    min_events_to_price: int = 30,
) -> Step | None:
    """The deepest step we can price at all, affordable or not.

    Used to say "this is what the closest workable target would cost" when
    nothing is affordable yet — a number to aim at beats "spend more".
    """
    priceable = [
        step
        for step in price_funnel(
            insights,
            events_to_exit_learning=events_to_exit_learning,
            min_events_to_price=min_events_to_price,
        )
        if step.priceable
    ]
    return priceable[-1] if priceable else None
