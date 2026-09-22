"""Planning and applying changes. No network: every response is a fake.

These tests carry more weight than the rest of the suite, because the code
under test is the only code in this project that can spend someone's money.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from metaaudit import apply as apply_mod
from metaaudit import plan as plan_mod
from metaaudit.api import GraphClient
from metaaudit.checks.base import CheckResult, Confidence, Finding, Severity
from metaaudit.fetch import AdSet, Campaign, Insights, Snapshot
from tests.test_api import FakeResponse


class RecordingSession:
    """Answers reads from a dict of node -> fields, records every write."""

    def __init__(self, nodes: dict[str, dict], fail_on: set[str] | None = None):
        self.nodes = nodes
        self.writes: list[tuple[str, dict]] = []
        self.fail_on = fail_on or set()

    def get(self, url, params=None, timeout=None):
        node_id = url.rstrip("/").rsplit("/", 1)[-1]
        return FakeResponse(200, dict(self.nodes.get(node_id, {})))

    def post(self, url, data=None, timeout=None):
        node_id = url.rstrip("/").rsplit("/", 1)[-1]
        payload = {k: v for k, v in (data or {}).items() if k != "access_token"}
        self.writes.append((node_id, payload))
        if node_id in self.fail_on:
            return FakeResponse(
                400, {"error": {"message": "nope", "code": 100}}
            )
        self.nodes.setdefault(node_id, {}).update(payload)
        return FakeResponse(200, {"success": True})


def client_for(session) -> GraphClient:
    return GraphClient("SECRET", "v23.0", session=session, sleep=lambda _: None)


def adset(aid, name, *, budget=100.0, campaign="c1", status="ACTIVE", spend=0.0,
          conversions=0.0) -> AdSet:
    return AdSet(
        id=aid, name=name, campaign_id=campaign, status=status,
        effective_status=status, daily_budget=budget,
        optimization_goal="OFFSITE_CONVERSIONS",
        insights=Insights(
            spend=spend,
            actions={"offsite_conversion.fb_pixel_purchase": conversions},
        ),
    )


def snapshot_with(adsets, *, campaign_budget=0.0, currency="USD") -> Snapshot:
    campaign = Campaign(
        id="c1", name="Campaign", status="ACTIVE", effective_status="ACTIVE",
        objective="OUTCOME_SALES", daily_budget=campaign_budget, adsets=adsets,
    )
    return Snapshot(
        account_id="act_1", account_name="Acct", currency=currency,
        timezone="Asia/Seoul", window_days=30,
        since="2026-01-01", until="2026-01-30",
        prev_since="2025-12-02", prev_until="2025-12-31",
        campaigns=[campaign],
    )


def self_competition(ids, names, *, tier="identical", confidence=Confidence.STRUCTURAL):
    return CheckResult(
        "structure.self_competition", "Self competition",
        findings=[Finding(
            check_id="structure.self_competition", severity=Severity.HIGH,
            confidence=confidence, title="dupes", detail="", recommendation="merge",
            level="campaign", entity_id="c1", entity_name="Campaign",
            evidence={
                "tier": tier,
                "adsets": [{"id": i, "name": n} for i, n in zip(ids, names)],
            },
        )],
    )


class TestOnlyStructuralFindingsBecomeChanges(unittest.TestCase):
    def test_measured_finding_is_never_applied(self):
        snap = snapshot_with([adset("a1", "one"), adset("a2", "two")])
        measured = self_competition(
            ["a1", "a2"], ["one", "two"], confidence=Confidence.MEASURED
        )
        built = plan_mod.build_plan(snap, [measured])
        self.assertEqual(built.changes, [])

    def test_heuristic_finding_is_never_applied(self):
        snap = snapshot_with([adset("a1", "one"), adset("a2", "two")])
        heuristic = self_competition(
            ["a1", "a2"], ["one", "two"], confidence=Confidence.HEURISTIC
        )
        self.assertEqual(plan_mod.build_plan(snap, [heuristic]).changes, [])

    def test_weaker_overlap_tier_goes_to_manual_not_to_changes(self):
        snap = snapshot_with([adset("a1", "one"), adset("a2", "two")])
        built = plan_mod.build_plan(
            snap, [self_competition(["a1", "a2"], ["one", "two"], tier="shared_footprint")]
        )
        self.assertEqual(built.changes, [])
        self.assertEqual(len(built.manual), 1)


class TestConsolidationIsFree(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot_with([
            adset("a1", "keeper", budget=100.0, spend=500.0, conversions=20),
            adset("a2", "dupe", budget=60.0, spend=400.0, conversions=3),
        ])
        self.built = plan_mod.build_plan(
            self.snap, [self_competition(["a1", "a2"], ["keeper", "dupe"])]
        )

    def test_the_better_performing_adset_survives(self):
        paused = [c for c in self.built.changes if c.kind == "pause"]
        self.assertEqual([c.entity_id for c in paused], ["a2"])

    def test_freed_budget_moves_to_the_survivor(self):
        budgets = [c for c in self.built.changes if c.kind == "budget"]
        self.assertEqual(len(budgets), 1)
        self.assertEqual(budgets[0].entity_id, "a1")
        # 100 + 60, in minor units for USD.
        self.assertEqual(budgets[0].after, 16000)

    def test_daily_spend_does_not_move(self):
        self.assertAlmostEqual(self.built.daily_delta, 0.0, places=6)


class TestCampaignBudgetOptimisation(unittest.TestCase):
    def test_no_adset_budget_is_written_under_cbo(self):
        snap = snapshot_with(
            [adset("a1", "keeper", budget=0.0, conversions=9),
             adset("a2", "dupe", budget=0.0)],
            campaign_budget=500.0,
        )
        built = plan_mod.build_plan(
            snap, [self_competition(["a1", "a2"], ["keeper", "dupe"])]
        )
        self.assertEqual([c.kind for c in built.changes], ["pause"])


def underbudgeted(aid, name, current, required):
    return CheckResult(
        "learning.underbudgeted", "Underbudgeted",
        findings=[Finding(
            check_id="learning.underbudgeted", severity=Severity.HIGH,
            confidence=Confidence.STRUCTURAL, title="t", detail="",
            recommendation="raise it", level="adset", entity_id=aid,
            entity_name=name,
            evidence={"daily_budget": current, "required_daily_budget": required,
                      "cpa": 50.0},
        )],
    )


class TestBudgetIncreasesNeedAnExplicitAllowance(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot_with([adset("a1", "starved", budget=100.0)])
        self.finding = underbudgeted("a1", "starved", 100.0, 357.0)

    def test_excluded_by_default(self):
        built = plan_mod.build_plan(self.snap, [self.finding])
        self.assertEqual(built.changes, [])
        self.assertEqual(len(built.manual), 1)
        self.assertIn("--allow-budget-increase", built.manual[0].why_manual)

    def test_included_once_allowed(self):
        built = plan_mod.build_plan(self.snap, [self.finding], budget_allowance=300.0)
        self.assertEqual(len(built.changes), 1)
        self.assertEqual(built.changes[0].after, 35700)
        self.assertAlmostEqual(built.daily_delta, 257.0, places=6)

    def test_allowance_is_a_total_not_a_per_adset_limit(self):
        snap = snapshot_with([
            adset("a1", "one", budget=100.0), adset("a2", "two", budget=100.0)
        ])
        results = [CheckResult(
            "learning.underbudgeted", "Underbudgeted",
            findings=[
                underbudgeted("a1", "one", 100.0, 300.0).findings[0],
                underbudgeted("a2", "two", 100.0, 300.0).findings[0],
            ],
        )]
        built = plan_mod.build_plan(snap, results, budget_allowance=250.0)
        # 200 each; the allowance covers one, so the second becomes manual.
        self.assertEqual(len(built.changes), 1)
        self.assertEqual(len(built.manual), 1)


class TestPlanFileIsNotATrustedInput(unittest.TestCase):
    def test_roundtrip_preserves_changes(self):
        snap = snapshot_with([
            adset("a1", "keeper", conversions=5), adset("a2", "dupe")
        ])
        built = plan_mod.build_plan(
            snap, [self_competition(["a1", "a2"], ["keeper", "dupe"])]
        )
        again = plan_mod.loads(plan_mod.dumps(built))
        self.assertEqual(len(again.changes), len(built.changes))
        self.assertEqual(again.changes[0].entity_id, built.changes[0].entity_id)

    def test_a_field_outside_the_allow_list_is_refused(self):
        snap = snapshot_with([adset("a1", "keeper", conversions=5), adset("a2", "d")])
        built = plan_mod.build_plan(
            snap, [self_competition(["a1", "a2"], ["keeper", "d"])]
        )
        payload = json.loads(plan_mod.dumps(built))
        payload["plan"]["changes"][0]["field_name"] = "name"
        with self.assertRaises(ValueError) as ctx:
            plan_mod.loads(json.dumps(payload))
        self.assertIn("refuses to apply", str(ctx.exception))

    def test_client_refuses_the_write_too(self):
        # Belt and braces: even if a plan slipped past, the client says no.
        client = client_for(RecordingSession({}))
        with self.assertRaises(ValueError):
            client.post("a1", {"name": "whatever"})


def one_pause_plan() -> plan_mod.Plan:
    snap = snapshot_with([
        adset("a1", "keeper", budget=100.0, conversions=9),
        adset("a2", "dupe", budget=0.0),
    ])
    return plan_mod.build_plan(
        snap, [self_competition(["a1", "a2"], ["keeper", "dupe"])]
    )


class TestReadBeforeWrite(unittest.TestCase):
    def test_a_drifted_object_is_skipped_not_forced(self):
        built = one_pause_plan()
        # Someone already paused it for another reason, or Meta did.
        session = RecordingSession({"a2": {"status": "ARCHIVED"}})
        outcomes = apply_mod.apply_plan(client_for(session), built)
        self.assertEqual(session.writes, [])
        states = {o.state for o in outcomes}
        self.assertIn("skipped", states)
        skipped = [o for o in outcomes if o.state == "skipped"][0]
        self.assertIn("stale", skipped.detail)

    def test_already_at_target_is_reported_as_unchanged(self):
        built = one_pause_plan()
        session = RecordingSession({"a2": {"status": "PAUSED"}})
        outcomes = apply_mod.apply_plan(client_for(session), built)
        self.assertEqual(session.writes, [])
        self.assertEqual([o.state for o in outcomes], ["unchanged"])

    def test_a_matching_object_is_written(self):
        built = one_pause_plan()
        session = RecordingSession({"a2": {"status": "ACTIVE"}})
        outcomes = apply_mod.apply_plan(client_for(session), built)
        self.assertEqual(session.writes, [("a2", {"status": "PAUSED"})])
        self.assertEqual([o.state for o in outcomes], ["applied"])

    def test_dry_run_reads_everything_and_writes_nothing(self):
        built = one_pause_plan()
        session = RecordingSession({"a2": {"status": "ACTIVE"}})
        outcomes = apply_mod.apply_plan(client_for(session), built, dry_run=True)
        self.assertEqual(session.writes, [])
        self.assertEqual([o.detail for o in outcomes], ["dry run"])

    def test_budget_compared_as_string_the_way_meta_returns_it(self):
        snap = snapshot_with([adset("a1", "starved", budget=100.0)])
        built = plan_mod.build_plan(
            snap, [underbudgeted("a1", "starved", 100.0, 300.0)],
            budget_allowance=500.0,
        )
        # Meta returns minor units as a string, the plan holds an int.
        session = RecordingSession({"a1": {"daily_budget": "10000"}})
        outcomes = apply_mod.apply_plan(client_for(session), built)
        self.assertEqual([o.state for o in outcomes], ["applied"])
        self.assertEqual(session.writes, [("a1", {"daily_budget": 30000})])


class TestFailuresAreReportedPerChange(unittest.TestCase):
    def test_one_failure_does_not_stop_the_rest(self):
        snap = snapshot_with([
            adset("a1", "keeper", budget=0.0, conversions=9),
            adset("a2", "dupe", budget=0.0),
            adset("a3", "dupe2", budget=0.0),
        ], campaign_budget=900.0)
        built = plan_mod.build_plan(
            snap, [self_competition(["a1", "a2", "a3"], ["keeper", "dupe", "dupe2"])]
        )
        self.assertEqual(len(built.changes), 2)
        session = RecordingSession(
            {"a2": {"status": "ACTIVE"}, "a3": {"status": "ACTIVE"}},
            fail_on={"a2"},
        )
        outcomes = apply_mod.apply_plan(client_for(session), built)
        states = sorted(o.state for o in outcomes)
        self.assertEqual(states, ["applied", "failed"])


class TestRollback(unittest.TestCase):
    def test_undo_file_exists_before_any_write_happens(self):
        built = one_pause_plan()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "undo.json"

            written_when_first_post_ran: list[bool] = []

            class WatchingSession(RecordingSession):
                def post(self, url, data=None, timeout=None):
                    written_when_first_post_ran.append(path.exists())
                    return super().post(url, data=data, timeout=timeout)

            session = WatchingSession({"a2": {"status": "ACTIVE"}})
            apply_mod.apply_plan(client_for(session), built, rollback_path=path)
            self.assertEqual(written_when_first_post_ran, [True])

    def test_undo_file_reverses_the_applied_changes(self):
        built = one_pause_plan()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "undo.json"
            session = RecordingSession({"a2": {"status": "ACTIVE"}})
            apply_mod.apply_plan(client_for(session), built, rollback_path=path)

            undo = plan_mod.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(undo.changes[0].entity_id, "a2")
            self.assertEqual(undo.changes[0].after, "ACTIVE")

            # Applying the undo puts the account back.
            outcomes = apply_mod.apply_plan(client_for(session), undo)
            self.assertEqual([o.state for o in outcomes], ["applied"])
            self.assertEqual(session.nodes["a2"]["status"], "ACTIVE")

    def test_rollback_plan_covers_only_what_actually_applied(self):
        built = one_pause_plan()
        session = RecordingSession({"a2": {"status": "ARCHIVED"}})
        outcomes = apply_mod.apply_plan(client_for(session), built)
        undo = apply_mod.rollback_plan(built, outcomes)
        self.assertEqual(undo.changes, [])


if __name__ == "__main__":
    unittest.main()
