"""The demo account ships with the tool, so it is pinned like shipped code.

A demo is a claim: "this is what the audit tells you". These tests keep that
claim true. If a check stops reporting what the demo was built to show, this
file fails and the demo gets fixed, rather than quietly becoming a tour of
findings the tool no longer makes.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from metaaudit import demo, snapshot_io
from metaaudit.checks import all_checks, run_all
from metaaudit.checks.learning import required_daily_budget
from metaaudit.cli import main
from metaaudit.config import Thresholds
from metaaudit.demo import PURCHASE, build_demo_account
from metaaudit.report import RENDERERS, collect


def _findings(snap=None):
    snap = snap or build_demo_account()
    return snap, collect(run_all(snap, Thresholds()))


class TestDemoAccount(unittest.TestCase):
    def setUp(self):
        self.snap, self.findings = _findings()

    def test_totals_reconcile_at_every_level(self):
        """An account whose own numbers disagree would teach the wrong thing."""
        for campaign in self.snap.campaigns:
            self.assertAlmostEqual(
                campaign.insights.spend,
                sum(a.insights.spend for a in campaign.adsets),
                places=2,
                msg=f"{campaign.id} spend does not match its ad sets",
            )
            self.assertEqual(
                campaign.insights.conversions([PURCHASE]),
                sum(a.insights.conversions([PURCHASE]) for a in campaign.adsets),
                f"{campaign.id} conversions do not match its ad sets",
            )
            for adset in campaign.adsets:
                self.assertAlmostEqual(
                    adset.insights.spend,
                    sum(ad.insights.spend for ad in adset.ads),
                    places=2,
                    msg=f"{adset.id} spend does not match its ads",
                )
        self.assertEqual(self.snap.account_insights.spend, 51_000_000)
        self.assertEqual(
            self.snap.account_insights.spend,
            sum(c.insights.spend for c in self.snap.campaigns),
        )
        self.assertEqual(self.snap.account_insights.conversions([PURCHASE]), 1012)

    def test_the_worked_example_from_the_readme_holds(self):
        """CPA 50,000 -> 357,143/day per ad set -> a 1,000,000 budget supports 2."""
        campaign = next(c for c in self.snap.campaigns if c.id == "demo-c1")
        cpa = campaign.insights.spend / campaign.insights.conversions([PURCHASE])
        self.assertEqual(cpa, 50_000.0)
        self.assertAlmostEqual(
            required_daily_budget(cpa, Thresholds()), 357_142.857, places=3
        )

        finding = self._one("structure.fragmentation", "demo-c1")
        self.assertEqual(finding.evidence["active_adsets"], 4)
        self.assertEqual(finding.evidence["cpa"], 50_000.0)
        self.assertEqual(finding.evidence["required_per_adset"], 357_142.86)
        self.assertEqual(finding.evidence["supportable_adsets"], 2)

        # Each of the four is starved, and the worst is flagged hardest.
        starved = [f for f in self.findings if f.check_id == "learning.underbudgeted"]
        self.assertEqual(
            {f.entity_id for f in starved if f.entity_id.startswith("demo-c1")},
            {"demo-c1-as1", "demo-c1-as2", "demo-c1-as3", "demo-c1-as4"},
        )
        worst = self._one("learning.underbudgeted", "demo-c1-as4")
        self.assertEqual(worst.severity.label, "HIGH")
        self.assertAlmostEqual(worst.evidence["achievable_weekly_events"], 24.2, places=1)

    def test_every_registered_check_produces_a_finding(self):
        """The demo is a tour: a check that fires on no demo data shows nothing."""
        registered = {check_id for check_id, _, _ in all_checks()}
        fired = {f.check_id for f in self.findings}
        self.assertEqual(registered - fired, set(), "checks the demo never triggers")

    def test_nothing_is_skipped(self):
        skipped = [r.check_id for r in run_all(self.snap, Thresholds()) if r.skipped_reason]
        self.assertEqual(skipped, [], "the demo should give every check enough to run")

    def test_the_correctly_configured_campaign_is_left_alone(self):
        """A tool that flags everything is as useless as one that flags nothing."""
        touched = [f for f in self.findings if f.entity_id.startswith("demo-c2")]
        self.assertEqual(
            [f"{f.check_id}:{f.entity_id}" for f in touched],
            [],
            "the healthy campaign should produce no findings",
        )
        utm = self._one("tracking.utm", self.snap.account_id)
        self.assertEqual(utm.evidence["untagged_ads"], 6)
        self.assertEqual(utm.evidence["checked_ads"], 25)
        for example in utm.evidence["examples"]:
            self.assertFalse(
                example["id"].startswith("demo-c2"),
                "the healthy campaign's ads are tagged and must not be listed",
            )

    def test_the_broken_tracking_is_reported_before_anything_is_paused(self):
        blind = [f for f in self.findings if f.entity_id == "demo-c3-as2"]
        self.assertIn("tracking.missing_pixel", {f.check_id for f in blind})
        self.assertIn("tracking.silent", {f.check_id for f in blind})
        # 12 conversions cannot carry a CPA, and the report says so rather
        # than ranking the ad set on it.
        undecidable = self._one("efficiency.undecidable", "demo-c3-as3")
        self.assertEqual(undecidable.evidence["conversions"], 12)
        self.assertEqual(undecidable.evidence["cpa"], 125_000.0)
        self.assertLess(undecidable.evidence["cpa_low"], 125_000.0)
        self.assertGreater(undecidable.evidence["cpa_high"], 125_000.0)

    def test_the_duplicate_pair_is_named(self):
        finding = next(
            f
            for f in self.findings
            if f.check_id == "structure.self_competition"
            and f.evidence.get("tier") == "identical"
        )
        self.assertEqual(
            {a["id"] for a in finding.evidence["adsets"]},
            {"demo-c1-as1", "demo-c1-as2"},
        )

    def test_it_is_labelled_as_synthetic_in_every_format(self):
        results = run_all(self.snap, Thresholds())
        for name, render in RENDERERS.items():
            with self.subTest(renderer=name):
                out = render(self.snap, results, Thresholds())
                self.assertIn("DEMO", out)
                self.assertIn("this account does not exist", out)

    def test_survives_a_snapshot_round_trip(self):
        reloaded = snapshot_io.loads(snapshot_io.dumps(self.snap))
        self.assertEqual(reloaded.account_id, self.snap.account_id)
        self.assertEqual(
            [f.check_id for f in collect(run_all(reloaded, Thresholds()))],
            [f.check_id for f in self.findings],
        )

    def _one(self, check_id: str, entity_id: str):
        matches = [
            f for f in self.findings if f.check_id == check_id and f.entity_id == entity_id
        ]
        self.assertEqual(len(matches), 1, f"expected one {check_id} on {entity_id}")
        return matches[0]


class TestDemoCli(unittest.TestCase):
    def test_runs_with_no_credentials_at_all(self):
        buf = io.StringIO()
        with unittest.mock.patch.dict(
            "os.environ",
            {"META_ACCESS_TOKEN": "", "META_AD_ACCOUNT_ID": ""},
            clear=False,
        ), redirect_stdout(buf):
            code = main(["--demo"])
        self.assertEqual(code, 0)
        self.assertIn(demo.ACCOUNT_ID, buf.getvalue())

    def test_never_constructs_an_api_client(self):
        """The point of the demo is that it touches nothing."""
        boom = unittest.mock.Mock(side_effect=AssertionError("the demo called the API"))
        with unittest.mock.patch("metaaudit.cli.GraphClient", boom):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(main(["--demo", "--format", "json"]), 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["scope"]["api_requests"], 0)
        self.assertTrue(payload["findings"])
        boom.assert_not_called()

    def test_conflicting_flags_are_rejected(self):
        for argv, expected in (
            (["--demo", "--from-snapshot", "x.json"], "--from-snapshot"),
            (["--demo", "--check-auth"], "--check-auth"),
            (["--demo", "--account", "act_1"], "--account"),
            (["--demo", "--window", "14"], "--window"),
        ):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(argv)
                self.assertEqual(code, 2)
                self.assertIn(expected, err.getvalue())

    def test_fail_on_gates_the_exit_code(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--demo"]), 0)
            self.assertEqual(main(["--demo", "--fail-on", "critical"]), 1)
            self.assertEqual(
                main(["--demo", "--only", "efficiency.undecidable", "--fail-on", "high"]),
                0,
            )

    def test_save_snapshot_writes_data_that_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.json"
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(["--demo", "--save-snapshot", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("safe to share", err.getvalue())
            self.assertEqual(snapshot_io.load(path).account_id, demo.ACCOUNT_ID)


if __name__ == "__main__":
    unittest.main()
