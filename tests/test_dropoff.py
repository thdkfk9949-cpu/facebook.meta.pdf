"""Locating the funnel bottleneck, and refusing to when the data cannot."""

from __future__ import annotations

import unittest

from metaaudit.checks.dropoff import check_funnel_dropoff
from metaaudit.config import Thresholds
from metaaudit.fetch import Campaign, Insights, Snapshot


def funnel(spend=150000.0, **counts) -> Insights:
    alias = {
        "clicks": "link_click",
        "lpv": "landing_page_view",
        "vc": "offsite_conversion.fb_pixel_view_content",
        "atc": "offsite_conversion.fb_pixel_add_to_cart",
        "ic": "offsite_conversion.fb_pixel_initiate_checkout",
        "purchase": "offsite_conversion.fb_pixel_purchase",
    }
    return Insights(
        spend=spend,
        impressions=20000,
        actions={alias[k]: float(v) for k, v in counts.items()},
    )


def snap_with(insights: Insights, *, delivering=True) -> Snapshot:
    status = "ACTIVE" if delivering else "PAUSED"
    campaign = Campaign(
        id="c1", name="Campaign", status=status, effective_status=status,
        objective="OUTCOME_SALES", daily_budget=10000.0, insights=insights,
    )
    return Snapshot(
        account_id="act_1", account_name="a", currency="KRW",
        timezone="Asia/Seoul", window_days=30,
        since="2026-08-23", until="2026-09-21",
        prev_since="2026-07-24", prev_until="2026-08-22",
        campaigns=[campaign], account_insights=insights,
    )


def run(insights, **overrides):
    return check_funnel_dropoff(snap_with(insights), Thresholds(**overrides))


class TestBottleneckSelection(unittest.TestCase):
    def test_finds_the_step_that_loses_the_most_people(self):
        # The real account: healthy until content view, then it falls off.
        result = run(funnel(clicks=111, lpv=79, vc=72, ic=1, purchase=1))
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.evidence["from_event"], "VIEW_CONTENT")
        self.assertEqual(finding.evidence["to_event"], "INITIATE_CHECKOUT")
        self.assertEqual(finding.evidence["upstream_count"], 72)
        self.assertEqual(finding.evidence["downstream_count"], 1)

    def test_a_steeper_rate_on_tiny_numbers_does_not_win(self):
        # 2 of 40 kept (5%) loses 38 people; 0 of 2 kept (0%) loses 2. The
        # worse *rate* is the second, the worse *problem* is the first.
        result = run(funnel(clicks=200, lpv=40, vc=2, ic=0, purchase=0))
        finding = result.findings[0]
        self.assertEqual(finding.evidence["from_event"], "LINK_CLICKS")
        self.assertEqual(finding.evidence["upstream_count"], 200)

    def test_the_diagnosis_matches_the_transition(self):
        result = run(funnel(clicks=200, lpv=30, vc=25, ic=20, purchase=18))
        self.assertIn("before the page rendered", result.findings[0].detail)


class TestItRefusesToGuess(unittest.TestCase):
    def test_a_thin_upstream_step_is_not_evaluated(self):
        # 20 clicks is not enough to bound a rate at the default threshold.
        result = run(funnel(clicks=20, lpv=2, vc=1))
        self.assertEqual(result.findings, [])
        self.assertIn("not yet delivered enough traffic", result.skipped_reason)

    def test_the_threshold_is_configurable(self):
        result = run(funnel(clicks=20, lpv=2, vc=1), funnel_min_upstream=10)
        self.assertEqual(len(result.findings), 1)

    def test_a_healthy_funnel_produces_nothing(self):
        result = run(funnel(clicks=200, lpv=180, vc=170, ic=120, purchase=100))
        self.assertEqual(result.findings, [])
        self.assertEqual(result.skipped_reason, "")

    def test_paused_campaigns_are_not_evaluated(self):
        insights = funnel(clicks=111, lpv=79, vc=72, ic=1)
        result = check_funnel_dropoff(
            snap_with(insights, delivering=False), Thresholds()
        )
        self.assertEqual(result.findings, [])
        self.assertIn("no campaign has", result.skipped_reason)


class TestTheClaimIsBounded(unittest.TestCase):
    def test_the_upper_bound_is_reported_not_just_the_point_rate(self):
        result = run(funnel(clicks=111, lpv=79, vc=72, ic=1, purchase=1))
        finding = result.findings[0]
        # Wilson on 1/72: roughly 0.2% to 7.5%.
        self.assertAlmostEqual(finding.evidence["rate_ci_high"], 0.0745, delta=0.005)
        self.assertGreater(finding.evidence["rate_ci_low"], 0.0)
        self.assertIn("no better than 7.5%", finding.detail)

    def test_the_point_rate_sits_inside_its_own_interval(self):
        result = run(funnel(clicks=400, lpv=100, vc=90, ic=80, purchase=70))
        ev = result.findings[0].evidence
        self.assertLessEqual(ev["rate_ci_low"], ev["rate"])
        self.assertGreaterEqual(ev["rate_ci_high"], ev["rate"])

    def test_severity_tracks_how_much_is_lost(self):
        severe = run(funnel(clicks=111, lpv=79, vc=72, ic=1)).findings[0]
        milder = run(funnel(clicks=200, lpv=70, vc=65, ic=60)).findings[0]
        self.assertGreater(int(severe.severity), int(milder.severity))


if __name__ == "__main__":
    unittest.main()
