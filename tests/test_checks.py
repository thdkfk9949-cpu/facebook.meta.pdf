"""Each check, against an account whose defects are known exactly."""

from __future__ import annotations

import unittest

from metaaudit.checks import run_all
from metaaudit.checks.base import Confidence, Severity
from metaaudit.checks.learning import required_daily_budget
from metaaudit.checks.overlap import audience_fingerprint
from metaaudit.config import Thresholds
from metaaudit.fetch import Ad, AdSet, Campaign, Snapshot
from tests.fixtures import (
    PIXEL,
    TARGETING_A,
    TARGETING_A_SHUFFLED,
    build_account,
    ins,
)


def findings_for(check_id, snap=None, th=None):
    snap = snap or build_account()
    th = th or Thresholds()
    results = run_all(snap, th, only=[check_id])
    return results[0].findings, results[0]


class TestLearningBudgetMath(unittest.TestCase):
    def test_required_budget_formula(self):
        # 50 events / 7 days at a CPA of 50 = 357.14 per day.
        self.assertAlmostEqual(required_daily_budget(50.0, Thresholds()), 357.142857, places=4)
        self.assertEqual(required_daily_budget(0.0, Thresholds()), 0.0)

    def test_starved_adsets_are_flagged_with_the_right_arithmetic(self):
        findings, _ = findings_for("learning.underbudgeted")
        by_name = {f.entity_name: f for f in findings}
        self.assertIn("c1-adset-1", by_name)
        f = by_name["c1-adset-1"]
        # CBO: 400/day across 4 ad sets = 100/day. CPA 50 -> 14 events/week.
        self.assertEqual(f.evidence["daily_budget"], 100.0)
        self.assertEqual(f.evidence["cpa"], 50.0)
        self.assertEqual(f.evidence["cpa_source"], "own")
        self.assertAlmostEqual(f.evidence["required_daily_budget"], 357.14, places=2)
        self.assertEqual(f.evidence["achievable_weekly_events"], 14.0)
        # No sample-size caveat: this is arithmetic about configuration.
        self.assertEqual(f.confidence, Confidence.STRUCTURAL)

    def test_well_funded_adset_is_not_flagged(self):
        snap = build_account()
        for adset in snap.campaigns[0].adsets:
            adset.daily_budget = 1000.0     # own budget beats the CBO split
        findings, _ = findings_for("learning.underbudgeted", snap)
        self.assertEqual([f for f in findings if f.entity_name.startswith("c1-")], [])

    def test_falls_back_to_campaign_cpa_when_adset_has_none(self):
        snap = build_account()
        target = snap.campaigns[0].adsets[0]
        target.insights = ins(spend=200, impressions=1000, clicks=50)  # no purchases
        findings, _ = findings_for("learning.underbudgeted", snap)
        f = next(x for x in findings if x.entity_name == "c1-adset-1")
        self.assertEqual(f.evidence["cpa_source"], "campaign")

    def test_skips_when_no_conversion_goal_anywhere(self):
        snap = build_account()
        for adset in snap.adsets:
            adset.optimization_goal = "LINK_CLICKS"
        _, result = findings_for("learning.underbudgeted", snap)
        self.assertIn("50-events/week rule does not apply", result.skipped_reason)


class TestLearningStage(unittest.TestCase):
    def test_learning_limited_is_high_severity(self):
        findings, _ = findings_for("learning.stuck")
        f = next(x for x in findings if x.entity_name == "c2-learning-limited")
        self.assertEqual(f.severity, Severity.HIGH)
        self.assertEqual(f.confidence, Confidence.MEASURED)

    def test_learning_in_progress_says_do_not_touch(self):
        snap = build_account()
        snap.campaigns[0].adsets[0].learning_stage_info = {"status": "LEARNING"}
        findings, _ = findings_for("learning.stuck", snap)
        f = next(x for x in findings if x.entity_name == "c1-adset-1")
        self.assertEqual(f.severity, Severity.LOW)
        self.assertIn("restarts learning", f.recommendation)


class TestFragmentation(unittest.TestCase):
    def test_reports_supportable_count(self):
        findings, _ = findings_for("structure.fragmentation")
        f = next(x for x in findings if x.entity_name == "Sales — fragmented")
        self.assertEqual(f.evidence["active_adsets"], 4)
        self.assertEqual(f.evidence["supportable_adsets"], 1)
        self.assertIn("budget supports 1", f.title)

    def test_zero_supportable_does_not_claim_one(self):
        # Regression: max(supportable, 1) in the title contradicted the body.
        findings, _ = findings_for("structure.fragmentation")
        f = next(x for x in findings if x.entity_name == "Sales — misconfigured")
        self.assertEqual(f.evidence["supportable_adsets"], 0)
        self.assertIn("none at this CPA", f.title)
        self.assertEqual(f.severity, Severity.HIGH)

    def test_single_adset_campaign_is_never_flagged(self):
        snap = build_account()
        snap.campaigns[0].adsets = snap.campaigns[0].adsets[:1]
        findings, _ = findings_for("structure.fragmentation", snap)
        self.assertEqual([f for f in findings if f.entity_id == "c1"], [])


class TestSelfCompetition(unittest.TestCase):
    def test_key_and_list_order_do_not_change_the_fingerprint(self):
        a = AdSet(id="a", name="a", campaign_id="c", status="ACTIVE",
                  effective_status="ACTIVE", targeting=dict(TARGETING_A))
        b = AdSet(id="b", name="b", campaign_id="c", status="ACTIVE",
                  effective_status="ACTIVE", targeting=dict(TARGETING_A_SHUFFLED))
        self.assertEqual(audience_fingerprint(a), audience_fingerprint(b))

    def test_identical_targeting_is_flagged(self):
        findings, _ = findings_for("structure.self_competition")
        self.assertTrue(findings)
        self.assertTrue(all(f.confidence == Confidence.STRUCTURAL for f in findings))

    def test_mutual_exclusion_clears_the_shared_footprint_finding(self):
        snap = build_account()
        campaign = snap.campaigns[0]
        campaign.adsets = campaign.adsets[:2]
        a, b = campaign.adsets
        # Same footprint, different audiences, each excluding the other's.
        a.targeting = {**TARGETING_A, "custom_audiences": [{"id": "A"}],
                       "excluded_custom_audiences": [{"id": "B"}]}
        b.targeting = {**TARGETING_A, "custom_audiences": [{"id": "B"}],
                       "excluded_custom_audiences": [{"id": "A"}]}
        findings, _ = findings_for("structure.self_competition", snap)
        self.assertEqual([f for f in findings if f.entity_id == "c1"], [])

    def test_shared_footprint_without_exclusion_is_a_heuristic_finding(self):
        snap = build_account()
        campaign = snap.campaigns[0]
        campaign.adsets = campaign.adsets[:2]
        a, b = campaign.adsets
        a.targeting = {**TARGETING_A, "custom_audiences": [{"id": "A"}]}
        b.targeting = {**TARGETING_A, "custom_audiences": [{"id": "B"}]}
        findings, _ = findings_for("structure.self_competition", snap)
        f = next(x for x in findings if x.evidence["tier"] == "shared_footprint")
        self.assertEqual(f.severity, Severity.MEDIUM)
        self.assertEqual(f.confidence, Confidence.HEURISTIC)
        self.assertIn("Audience Overlap tool", f.detail)

    def test_one_sided_exclusion_is_not_mutual(self):
        snap = build_account()
        campaign = snap.campaigns[0]
        campaign.adsets = campaign.adsets[:2]
        a, b = campaign.adsets
        a.targeting = {**TARGETING_A, "custom_audiences": [{"id": "A"}],
                       "excluded_custom_audiences": [{"id": "B"}]}
        b.targeting = {**TARGETING_A, "custom_audiences": [{"id": "B"}]}
        findings, _ = findings_for("structure.self_competition", snap)
        self.assertTrue([f for f in findings if f.entity_id == "c1"])

    def test_placement_only_differences_still_count_as_the_same_audience(self):
        snap = build_account()
        snap.campaigns[0].adsets[0].targeting = {
            **TARGETING_A, "publisher_platforms": ["facebook"]
        }
        snap.campaigns[0].adsets[1].targeting = {
            **TARGETING_A, "publisher_platforms": ["instagram"]
        }
        findings, _ = findings_for("structure.self_competition", snap)
        self.assertTrue(
            any("c1-adset-1" in {m["name"] for m in f.evidence["adsets"]} for f in findings)
        )


class TestFatigue(unittest.TestCase):
    def test_frequency_plus_significant_ctr_drop(self):
        findings, _ = findings_for("delivery.fatigue")
        f = next(x for x in findings if x.entity_name == "c2-fatigued")
        self.assertEqual(f.severity, Severity.HIGH)     # frequency 5.2 >= 4.5
        self.assertEqual(f.confidence, Confidence.MEASURED)
        self.assertAlmostEqual(f.evidence["ctr_decline_pct"], 50.0, places=1)
        self.assertLess(f.evidence["p_value"], 0.05)

    def test_high_frequency_without_a_ctr_drop_is_not_fatigue(self):
        snap = build_account()
        tired = next(a for a in snap.adsets if a.id == "as22")
        tired.prev_insights = ins(spend=4000, impressions=400_000, clicks=4_000, frequency=2.1)
        findings, _ = findings_for("delivery.fatigue", snap)
        self.assertEqual([f for f in findings if f.entity_name == "c2-fatigued"], [])

    def test_ctr_drop_below_frequency_threshold_is_ignored(self):
        snap = build_account()
        tired = next(a for a in snap.adsets if a.id == "as22")
        tired.insights.frequency = 1.2
        findings, _ = findings_for("delivery.fatigue", snap)
        self.assertEqual([f for f in findings if f.entity_name == "c2-fatigued"], [])

    def test_tiny_samples_are_reported_as_untestable_not_as_fatigue(self):
        snap = build_account()
        tired = next(a for a in snap.adsets if a.id == "as22")
        tired.insights = ins(spend=50, impressions=100, clicks=1, frequency=5.0)
        tired.prev_insights = ins(spend=50, impressions=100, clicks=4, frequency=2.0)
        findings, _ = findings_for(
            "delivery.fatigue", snap, Thresholds(min_spend_share_to_report=0.0)
        )
        f = next(x for x in findings if x.entity_name == "c2-fatigued")
        self.assertEqual(f.severity, Severity.INFO)
        self.assertIn("too few impressions", f.title)


class TestCreativeSupply(unittest.TestCase):
    def test_single_ad_adset_is_flagged(self):
        findings, _ = findings_for("creative.supply")
        f = next(x for x in findings if x.entity_name == "c2-buying-clicks")
        self.assertEqual(f.evidence["active_ads"], 1)

    def test_near_duplicate_ads_are_flagged_despite_the_count(self):
        snap = build_account()
        target = next(a for a in snap.adsets if a.id == "as22")
        for ad in target.ads:
            ad.creative["object_story_spec"]["link_data"]["message"] = "Same   TEXT"
        findings, _ = findings_for("creative.supply", snap)
        f = next(x for x in findings if x.entity_name == "c2-fatigued")
        self.assertEqual(f.evidence["distinct_primary_texts"], 1)

    def test_dynamic_creative_adsets_are_exempt(self):
        snap = build_account()
        target = next(a for a in snap.adsets if a.id == "as21")
        target.is_dynamic_creative = True
        findings, _ = findings_for("creative.supply", snap)
        self.assertEqual([f for f in findings if f.entity_name == "c2-buying-clicks"], [])


class TestTracking(unittest.TestCase):
    def test_click_optimisation_in_a_sales_campaign_is_critical(self):
        findings, _ = findings_for("tracking.goal_mismatch")
        f = findings[0]
        self.assertEqual(f.severity, Severity.CRITICAL)
        self.assertEqual(f.entity_name, "c2-buying-clicks")

    def test_missing_promoted_object_is_critical(self):
        snap = build_account()
        target = next(a for a in snap.adsets if a.id == "as22")
        target.promoted_object = {}
        findings, _ = findings_for("tracking.missing_pixel", snap)
        self.assertEqual(findings[0].severity, Severity.CRITICAL)

    def test_pixel_without_event_type_is_flagged(self):
        snap = build_account()
        target = next(a for a in snap.adsets if a.id == "as22")
        target.promoted_object = {"pixel_id": "999"}     # no custom_event_type
        findings, _ = findings_for("tracking.missing_pixel", snap)
        self.assertTrue(any(f.entity_name == "c2-fatigued" for f in findings))

    def test_properly_configured_adsets_produce_nothing(self):
        findings, _ = findings_for("tracking.missing_pixel")
        self.assertEqual(findings, [])

    def test_account_wide_silence_is_critical(self):
        snap = build_account()
        for adset in snap.adsets:
            adset.insights.actions = {}
        findings, _ = findings_for("tracking.silent", snap)
        self.assertEqual(findings[0].severity, Severity.CRITICAL)
        self.assertEqual(findings[0].level, "account")

    def test_mixed_attribution_windows_are_flagged(self):
        findings, _ = findings_for("tracking.attribution_mix")
        self.assertTrue(any(f.entity_name == "Sales — misconfigured" for f in findings))

    def test_uniform_attribution_is_clean(self):
        snap = build_account()
        for adset in snap.adsets:
            adset.attribution_spec = [{"event_type": "CLICK_THROUGH", "window_days": 7}]
        findings, _ = findings_for("tracking.attribution_mix", snap)
        self.assertEqual(findings, [])


class TestUtm(unittest.TestCase):
    def test_counts_only_ads_with_outbound_links(self):
        findings, _ = findings_for("tracking.utm")
        f = findings[0]
        self.assertEqual(f.evidence["untagged_ads"], 1)
        self.assertEqual(f.evidence["checked_ads"], 19)

    def test_url_tags_field_counts_as_tagging(self):
        snap = build_account()
        bad = next(a for a in snap.ads if a.id == "as21-a1")
        bad.creative["url_tags"] = "utm_source=fb&utm_medium=paid&utm_campaign=c"
        findings, _ = findings_for("tracking.utm", snap)
        self.assertEqual(findings, [])

    def test_skips_when_no_ad_has_a_link(self):
        snap = build_account()
        for ad in snap.ads:
            ad.creative["object_story_spec"]["link_data"]["link"] = ""
        _, result = findings_for("tracking.utm", snap)
        self.assertIn("no delivering ads", result.skipped_reason)


class TestEfficiencyGuardrails(unittest.TestCase):
    def test_small_samples_never_produce_an_outlier_verdict(self):
        # c2's two conversion ad sets differ, but 6 conversions cannot prove it.
        findings, _ = findings_for("efficiency.outliers")
        self.assertEqual(
            [f for f in findings if f.entity_name == "c2-learning-limited"], []
        )

    def test_a_real_difference_on_a_big_sample_is_reported(self):
        snap = build_account()
        loser = snap.campaigns[0].adsets[0]
        loser.insights = ins(spend=1250, impressions=200_000, clicks=4000, purchases=2)
        findings, _ = findings_for("efficiency.outliers", snap)
        f = next(x for x in findings if x.entity_name == "c1-adset-1")
        self.assertLess(f.evidence["p_value"], 0.05)
        self.assertIn("check the learning findings first", f.recommendation)

    def test_undecidable_reports_a_confidence_band_not_a_verdict(self):
        findings, _ = findings_for("efficiency.undecidable")
        f = next(x for x in findings if x.entity_name == "c1-adset-1")
        self.assertEqual(f.severity, Severity.INFO)
        self.assertLess(f.evidence["cpa_low"], f.evidence["cpa"])
        self.assertGreater(f.evidence["cpa_high"], f.evidence["cpa"])


class TestRobustness(unittest.TestCase):
    def test_empty_account_produces_no_findings_and_no_crash(self):
        empty = Snapshot(
            account_id="act_0", account_name="Empty", currency="KRW", timezone="",
            window_days=30, since="2026-08-22", until="2026-09-20",
            prev_since="2026-07-23", prev_until="2026-08-21",
        )
        results = run_all(empty, Thresholds())
        self.assertEqual([f for r in results for f in r.findings], [])

    def test_a_raising_check_is_contained(self):
        snap = build_account()
        # A targeting value the canonicaliser cannot sort must not kill the run.
        snap.campaigns[0].adsets[0].targeting = {"geo_locations": object()}
        results = run_all(snap, Thresholds())
        self.assertTrue(results, "the run must still return results")

    def test_paused_entities_are_ignored(self):
        snap = build_account()
        for campaign in snap.campaigns:
            campaign.effective_status = "PAUSED"
            for adset in campaign.adsets:
                adset.effective_status = "PAUSED"
        results = run_all(snap, Thresholds())
        structural = [
            f
            for r in results
            for f in r.findings
            if f.check_id.startswith(("learning.", "structure.", "delivery."))
        ]
        self.assertEqual(structural, [])


if __name__ == "__main__":
    unittest.main()


class TestSelfCompetitionSkipReasons(unittest.TestCase):
    """A skip must name the real reason — they lead to different actions."""

    def _snap(self, adsets):
        from metaaudit.fetch import Campaign, Snapshot

        return Snapshot(
            account_id="act_1", account_name="a", currency="KRW",
            timezone="Asia/Seoul", window_days=30,
            since="2026-01-01", until="2026-01-30",
            prev_since="2025-12-02", prev_until="2025-12-31",
            campaigns=[Campaign(
                id="c1", name="c", status="ACTIVE", effective_status="ACTIVE",
                adsets=adsets,
            )],
        )

    def _adset(self, aid, targeting):
        from metaaudit.fetch import AdSet

        return AdSet(
            id=aid, name=aid, campaign_id="c1", status="ACTIVE",
            effective_status="ACTIVE", targeting=targeting,
        )

    def test_one_adset_is_not_reported_as_a_permission_problem(self):
        # Regression: a single-ad-set account was told its token might lack
        # permission to read targeting, which sent the reader looking for a
        # problem that was not there.
        from metaaudit.checks.overlap import check_self_competition
        from metaaudit.config import Thresholds

        snap = self._snap([self._adset("a1", {"age_min": 25})])
        result = check_self_competition(snap, Thresholds())
        self.assertIn("two or more delivering ad sets", result.skipped_reason)
        self.assertNotIn("permission", result.skipped_reason)

    def test_missing_targeting_still_names_permissions(self):
        from metaaudit.checks.overlap import check_self_competition
        from metaaudit.config import Thresholds

        snap = self._snap([self._adset("a1", {}), self._adset("a2", {})])
        result = check_self_competition(snap, Thresholds())
        self.assertIn("permission", result.skipped_reason)
