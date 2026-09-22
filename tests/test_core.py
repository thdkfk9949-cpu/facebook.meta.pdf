"""Currency, config, stats, and snapshot round-tripping."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from metaaudit import snapshot_io
from metaaudit.config import Settings, Thresholds, load_env_file
from metaaudit.currency import fmt, offset_for, to_major
from metaaudit.fetch import AdSet, Campaign, Insights, _windows
from metaaudit.stats import (
    assess,
    cpa_relative_margin,
    two_proportion_p,
    wilson_interval,
)
from tests.fixtures import build_account


class TestCurrency(unittest.TestCase):
    def test_minor_unit_offsets(self):
        self.assertEqual(offset_for("USD"), 100)
        self.assertEqual(offset_for("krw"), 1)
        self.assertEqual(offset_for("JPY"), 1)
        self.assertEqual(offset_for("KWD"), 1000)
        self.assertEqual(offset_for(""), 100)

    def test_to_major_respects_currency(self):
        # The bug this guards: 10000 is 100.00 USD but 10,000 KRW.
        self.assertEqual(to_major(10000, "USD"), 100.0)
        self.assertEqual(to_major(10000, "KRW"), 10000.0)
        self.assertEqual(to_major(None, "USD"), 0.0)

    def test_fmt_drops_decimals_for_zero_decimal_currencies(self):
        self.assertEqual(fmt(10000.0, "KRW"), "10,000 KRW")
        self.assertEqual(fmt(100.0, "USD"), "100.00 USD")


class TestSettings(unittest.TestCase):
    def test_account_id_normalisation(self):
        self.assertEqual(Settings.normalize_account_id("act_123"), "act_123")
        self.assertEqual(Settings.normalize_account_id("123"), "act_123")
        self.assertEqual(Settings.normalize_account_id(" act_123 "), "act_123")

    def test_rejects_non_numeric_account(self):
        with self.assertRaises(ValueError):
            Settings.normalize_account_id("act_abc")
        with self.assertRaises(ValueError):
            Settings.normalize_account_id("")

    def test_from_env_requires_token(self):
        with self.assertRaises(SystemExit):
            Settings.from_env(env={"META_AD_ACCOUNT_ID": "act_1"})

    def test_from_env_reads_values(self):
        s = Settings.from_env(
            env={
                "META_ACCESS_TOKEN": "t",
                "META_AD_ACCOUNT_ID": "999",
                "META_API_VERSION": "v24.0",
            }
        )
        self.assertEqual(s.ad_account_id, "act_999")
        self.assertEqual(s.api_version, "v24.0")


class TestEnvFile(unittest.TestCase):
    def test_parses_quotes_comments_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n"
                "\n"
                "META_ACCESS_TOKEN='abc$def'\n"
                'export META_AD_ACCOUNT_ID="act_5"\n'
                "META_API_VERSION=v23.0\n",
                encoding="utf-8",
            )
            env = load_env_file(path)
        # No interpolation: a token containing $ survives intact.
        self.assertEqual(env["META_ACCESS_TOKEN"], "abc$def")
        self.assertEqual(env["META_AD_ACCOUNT_ID"], "act_5")
        self.assertEqual(env["META_API_VERSION"], "v23.0")

    def test_rejects_malformed_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("NOT_A_PAIR\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_env_file(path)


class TestThresholds(unittest.TestCase):
    def test_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"nonsense": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                Thresholds.load(path)

    def test_overrides_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"frequency_warn": 9.0}), encoding="utf-8")
            self.assertEqual(Thresholds.load(path).frequency_warn, 9.0)


class TestStats(unittest.TestCase):
    def test_large_real_difference_is_significant(self):
        p = two_proportion_p(200, 10_000, 100, 10_000)
        self.assertIsNotNone(p)
        self.assertLess(p, 0.001)

    def test_noise_is_not_significant(self):
        p = two_proportion_p(100, 10_000, 102, 10_000)
        self.assertGreater(p, 0.05)

    def test_returns_none_when_approximation_invalid(self):
        # Fewer than 5 expected successes: must refuse rather than guess.
        self.assertIsNone(two_proportion_p(2, 50, 1, 50))
        self.assertIsNone(two_proportion_p(0, 0, 0, 0))

    def test_wilson_interval_is_sane_at_small_n(self):
        low, high = wilson_interval(5, 100)
        self.assertTrue(0 < low < 0.05 < high < 0.2)
        # Never escapes [0, 1] even at the extremes.
        self.assertEqual(wilson_interval(0, 10)[0], 0.0)
        self.assertLessEqual(wilson_interval(10, 10)[1], 1.0)

    def test_cpa_margin_shrinks_with_conversions(self):
        self.assertAlmostEqual(cpa_relative_margin(100), 0.196, places=3)
        self.assertGreater(cpa_relative_margin(10), cpa_relative_margin(100))
        self.assertIsNone(cpa_relative_margin(0))

    def test_assess_reports_shortfall(self):
        self.assertFalse(assess(3, 30))
        self.assertTrue(assess(30, 30))
        self.assertIn("need 30", assess(3, 30).reason)


class TestWindows(unittest.TestCase):
    def test_window_ends_yesterday_and_baseline_is_contiguous(self):
        since, until, prev_since, prev_until = _windows(30, dt.date(2026, 9, 21))
        self.assertEqual(until, "2026-09-20")          # not today
        self.assertEqual(since, "2026-08-22")
        self.assertEqual(prev_until, "2026-08-21")     # day before `since`
        self.assertEqual(prev_since, "2026-07-23")
        span = dt.date.fromisoformat(until) - dt.date.fromisoformat(since)
        prev_span = dt.date.fromisoformat(prev_until) - dt.date.fromisoformat(prev_since)
        self.assertEqual(span, prev_span)              # equal length


class TestInsights(unittest.TestCase):
    def test_conversion_actions_are_not_double_counted(self):
        ins = Insights.from_row(
            {
                "spend": "100.50",
                "impressions": "1000",
                "actions": [
                    {"action_type": "purchase", "value": "5"},
                    {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "5"},
                ],
            }
        )
        # Both aliases describe the same 5 purchases; summing would report 10.
        self.assertEqual(
            ins.conversions(("offsite_conversion.fb_pixel_purchase", "purchase")), 5.0
        )
        self.assertEqual(ins.spend, 100.5)

    def test_missing_fields_become_zero(self):
        ins = Insights.from_row({})
        self.assertEqual((ins.spend, ins.impressions, ins.clicks), (0.0, 0, 0))


class TestEffectiveBudget(unittest.TestCase):
    def test_cbo_budget_is_split_across_active_adsets(self):
        campaign = Campaign(
            id="c", name="c", status="ACTIVE", effective_status="ACTIVE",
            daily_budget=400.0,
        )
        adsets = [
            AdSet(id=str(i), name=str(i), campaign_id="c", status="ACTIVE",
                  effective_status="ACTIVE")
            for i in range(4)
        ]
        campaign.adsets = adsets
        self.assertEqual(adsets[0].effective_daily_budget(campaign), 100.0)

    def test_own_budget_wins_over_campaign_budget(self):
        campaign = Campaign(
            id="c", name="c", status="ACTIVE", effective_status="ACTIVE",
            daily_budget=400.0,
        )
        adset = AdSet(id="a", name="a", campaign_id="c", status="ACTIVE",
                      effective_status="ACTIVE", daily_budget=77.0)
        campaign.adsets = [adset]
        self.assertEqual(adset.effective_daily_budget(campaign), 77.0)

    def test_lifetime_budget_has_no_daily_figure(self):
        campaign = Campaign(id="c", name="c", status="ACTIVE", effective_status="ACTIVE")
        adset = AdSet(id="a", name="a", campaign_id="c", status="ACTIVE",
                      effective_status="ACTIVE", lifetime_budget=5000.0)
        campaign.adsets = [adset]
        self.assertEqual(adset.effective_daily_budget(campaign), 0.0)


class TestSnapshotIO(unittest.TestCase):
    def test_round_trip_preserves_everything_the_checks_read(self):
        original = build_account()
        restored = snapshot_io.loads(snapshot_io.dumps(original))
        self.assertEqual(len(restored.campaigns), len(original.campaigns))
        self.assertEqual(len(restored.adsets), len(original.adsets))
        self.assertEqual(len(restored.ads), len(original.ads))
        self.assertEqual(restored.currency, original.currency)
        self.assertEqual(
            restored.account_insights.spend, original.account_insights.spend
        )
        a_before = original.adsets[0]
        a_after = restored.adsets[0]
        self.assertEqual(a_after.targeting, a_before.targeting)
        self.assertEqual(a_after.prev_insights.clicks, a_before.prev_insights.clicks)
        self.assertEqual(a_after.learning_stage_info, a_before.learning_stage_info)

    def test_rejects_unknown_schema_version(self):
        with self.assertRaises(ValueError):
            snapshot_io.loads(json.dumps({"schema_version": 99, "snapshot": {}}))


if __name__ == "__main__":
    unittest.main()


class TestPoissonIntervals(unittest.TestCase):
    """Exact bounds, checked against published Garwood tables."""

    def test_matches_published_95_percent_values(self):
        from metaaudit.stats import poisson_interval

        for count, (lo, hi) in [
            (0, (0.000, 3.689)),
            (1, (0.025, 5.572)),
            (5, (1.623, 11.668)),
            (10, (4.795, 18.390)),
            (100, (81.36, 121.63)),
        ]:
            got_lo, got_hi = poisson_interval(count)
            self.assertAlmostEqual(got_lo, lo, places=2, msg=f"lower at n={count}")
            self.assertAlmostEqual(got_hi, hi, places=2, msg=f"upper at n={count}")

    def test_lower_bound_is_never_negative(self):
        from metaaudit.stats import poisson_interval

        for count in range(0, 40):
            lo, hi = poisson_interval(count)
            self.assertGreaterEqual(lo, 0.0, f"n={count}")
            self.assertLess(lo, hi, f"n={count}")


class TestCpaInterval(unittest.TestCase):
    def test_a_single_conversion_gives_a_positive_ordered_interval(self):
        # Regression: the symmetric form reported a CPA "between -78,934 and
        # 243,380 KRW" off one conversion. A CPA cannot be negative, and the
        # real upper bound is an order of magnitude higher than that.
        from metaaudit.stats import cpa_interval

        low, high = cpa_interval(82223, 1)
        self.assertGreater(low, 0)
        self.assertLess(low, 82223)
        self.assertGreater(high, 82223)
        self.assertAlmostEqual(low, 82223 / 5.5716, delta=50)
        self.assertAlmostEqual(high, 82223 / 0.02532, delta=20000)

    def test_the_point_estimate_always_sits_inside(self):
        from metaaudit.stats import cpa_interval

        for conv in range(1, 60):
            spend = 1000.0 * conv
            low, high = cpa_interval(spend, conv)
            self.assertLessEqual(low, spend / conv)
            self.assertGreaterEqual(high, spend / conv)

    def test_the_interval_narrows_as_conversions_accumulate(self):
        from metaaudit.stats import cpa_interval

        widths = []
        for conv in (2, 10, 50, 200):
            low, high = cpa_interval(1000.0 * conv, conv)
            widths.append((high - low) / (1000.0))
        self.assertEqual(widths, sorted(widths, reverse=True))

    def test_no_conversions_means_no_interval_not_a_made_up_one(self):
        from metaaudit.stats import cpa_interval

        self.assertIsNone(cpa_interval(50000, 0))


class TestNoFieldIsFetchedAndThrownAway(unittest.TestCase):
    """Every field asked of Meta must land somewhere, or be listed as unused.

    Regression: ADSET_FIELDS requested created_time, start_time and end_time,
    the model kept none of them, and the snapshot could not answer "did this
    ad set run for the whole window?" — the question its own spend numbers
    raised. Paying for a field and discarding it is the quiet kind of bug.
    """

    # Fields fetched for a reason other than storage, with that reason.
    DELIBERATELY_UNSTORED = {
        # Read to build the snapshot header, not kept on the entity.
        "account": {"id", "timezone_name", "amount_spent", "account_status",
                    "disable_reason", "spend_cap", "currency", "name"},
        # Nested creative is flattened into Ad fields by the fetcher.
        "ad": {"creative{id,name,url_tags,object_story_spec,asset_feed_spec,"
               "effective_object_story_id,degrees_of_freedom_spec,status}"},
        "campaign": set(),
        "adset": set(),
    }

    def _stored(self, cls):
        import dataclasses

        return {f.name for f in dataclasses.fields(cls)}

    def test_every_adset_field_is_stored(self):
        from metaaudit.fetch import ADSET_FIELDS, AdSet

        missing = (
            set(ADSET_FIELDS)
            - self._stored(AdSet)
            - self.DELIBERATELY_UNSTORED["adset"]
        )
        self.assertEqual(missing, set(), f"fetched but discarded: {sorted(missing)}")

    def test_every_campaign_field_is_stored(self):
        from metaaudit.fetch import CAMPAIGN_FIELDS, Campaign

        missing = (
            set(CAMPAIGN_FIELDS)
            - self._stored(Campaign)
            - self.DELIBERATELY_UNSTORED["campaign"]
        )
        self.assertEqual(missing, set(), f"fetched but discarded: {sorted(missing)}")
