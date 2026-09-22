"""Funnel pricing, and the recommendation it drives."""

from __future__ import annotations

import unittest

from metaaudit.checks.learning import check_underbudgeted
from metaaudit.config import Thresholds
from metaaudit.fetch import AdSet, Campaign, Insights, Snapshot
from metaaudit.funnel import cheapest_reachable, nearest_priceable, price_funnel


def insights(spend, **actions) -> Insights:
    return Insights(spend=spend, impressions=10000, actions=dict(actions))


# The real account this was built from: 150,688 KRW over ten days, a funnel
# that is healthy until it collapses at checkout.
REAL = insights(
    150688.0,
    link_click=111.0,
    landing_page_view=79.0,
    **{
        "offsite_conversion.fb_pixel_view_content": 72.0,
        "offsite_conversion.fb_pixel_initiate_checkout": 1.0,
        "offsite_conversion.fb_pixel_purchase": 1.0,
    },
)


class TestPricing(unittest.TestCase):
    def test_steps_come_back_shallowest_first(self):
        events = [s.event for s in price_funnel(REAL)]
        self.assertEqual(
            events,
            ["LINK_CLICKS", "LANDING_PAGE_VIEWS", "VIEW_CONTENT",
             "INITIATE_CHECKOUT", "PURCHASE"],
        )

    def test_cost_and_required_budget(self):
        by_event = {s.event: s for s in price_funnel(REAL)}
        vc = by_event["VIEW_CONTENT"]
        self.assertAlmostEqual(vc.cost, 150688 / 72, places=2)
        self.assertAlmostEqual(vc.required_daily, (150688 / 72) * 50 / 7, places=2)

    def test_a_single_observation_is_not_priceable(self):
        by_event = {s.event: s for s in price_funnel(REAL)}
        self.assertFalse(by_event["PURCHASE"].priceable)
        self.assertFalse(by_event["INITIATE_CHECKOUT"].priceable)
        self.assertTrue(by_event["VIEW_CONTENT"].priceable)

    def test_pixel_scoped_action_wins_over_the_bare_alias(self):
        # Both fire for one event; summing them would double the count and
        # halve the cost.
        both = insights(
            1000.0,
            **{
                "offsite_conversion.fb_pixel_view_content": 10.0,
                "omni_view_content": 10.0,
                "view_content": 10.0,
            },
        )
        step = [s for s in price_funnel(both) if s.event == "VIEW_CONTENT"][0]
        self.assertEqual(step.count, 10)


class TestReachability(unittest.TestCase):
    def test_picks_the_deepest_affordable_step(self):
        # At 20,000/day, VIEW_CONTENT (14,949) fits and is deeper than clicks.
        step = cheapest_reachable(REAL, 20000)
        self.assertEqual(step.event, "VIEW_CONTENT")

    def test_only_clicks_fit_the_real_budget(self):
        step = cheapest_reachable(REAL, 10000)
        self.assertEqual(step.event, "LINK_CLICKS")

    def test_nothing_affordable_returns_none_not_a_guess(self):
        self.assertIsNone(cheapest_reachable(REAL, 100))

    def test_nearest_priceable_ignores_affordability_but_not_sample_size(self):
        step = nearest_priceable(REAL)
        self.assertEqual(step.event, "VIEW_CONTENT")  # not PURCHASE, n=1


def account(objective="OUTCOME_SALES", adset_count=1, campaign_budget=10000.0):
    adsets = [
        AdSet(
            id=f"a{i}", name=f"adset {i}", campaign_id="c1", status="ACTIVE",
            effective_status="ACTIVE", optimization_goal="OFFSITE_CONVERSIONS",
            promoted_object={"custom_event_type": "PURCHASE"},
            insights=REAL if i == 0 else insights(1000.0),
        )
        for i in range(adset_count)
    ]
    campaign = Campaign(
        id="c1", name="c", status="ACTIVE", effective_status="ACTIVE",
        objective=objective, daily_budget=campaign_budget, adsets=adsets,
        insights=REAL,
    )
    return Snapshot(
        account_id="act_1", account_name="a", currency="KRW",
        timezone="Asia/Seoul", window_days=30,
        since="2026-08-23", until="2026-09-21",
        prev_since="2026-07-24", prev_until="2026-08-22",
        campaigns=[campaign], account_insights=REAL,
    )


class TestRecommendationMatchesTheAccount(unittest.TestCase):
    def _fix(self, snap):
        result = check_underbudgeted(snap, Thresholds())
        self.assertTrue(result.findings, "expected an underbudgeted finding")
        return result.findings[0].recommendation

    def test_no_sibling_is_never_suggested_when_there_is_none(self):
        # Regression: a one-ad-set account was told to "merge it into a
        # sibling ad set", in a report that said on the next line it had one
        # ad set in total.
        fix = self._fix(account(adset_count=1))
        self.assertNotIn("sibling", fix)
        self.assertNotIn("Consolidating", fix)

    def test_consolidation_is_offered_when_siblings_exist(self):
        fix = self._fix(account(adset_count=3))
        self.assertIn("Consolidating", fix)

    def test_clicks_are_not_recommended_inside_a_sales_campaign(self):
        # tracking.goal_mismatch calls exactly this CRITICAL; the tool must
        # not prescribe it one finding later.
        fix = self._fix(account(objective="OUTCOME_SALES"))
        self.assertNotIn("Optimise for LINK_CLICKS instead", fix)
        self.assertIn("not buyers", fix)

    def test_the_deeper_event_is_priced_with_the_budget_delta(self):
        fix = self._fix(account(objective="OUTCOME_SALES"))
        self.assertIn("VIEW_CONTENT", fix)
        self.assertIn("14,949 KRW/day", fix)
        self.assertIn("+4,949 KRW", fix)

    def test_a_traffic_campaign_may_be_told_to_buy_clicks(self):
        fix = self._fix(account(objective="OUTCOME_TRAFFIC"))
        self.assertIn("Optimise for LINK_CLICKS instead", fix)


if __name__ == "__main__":
    unittest.main()
