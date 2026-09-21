"""End-to-end: snapshot on disk -> checks -> rendered report, no network."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from metaaudit import snapshot_io
from metaaudit.checks import run_all
from metaaudit.cli import main
from metaaudit.config import Thresholds
from metaaudit.report import RENDERERS, collect
from tests.fixtures import build_account


class TestRenderers(unittest.TestCase):
    def setUp(self):
        self.snap = build_account()
        self.results = run_all(self.snap, Thresholds())

    def test_every_renderer_produces_output(self):
        for name, render in RENDERERS.items():
            with self.subTest(renderer=name):
                out = render(self.snap, self.results, Thresholds())
                self.assertTrue(out.strip(), f"{name} produced nothing")

    def test_json_is_valid_and_carries_the_findings(self):
        payload = json.loads(RENDERERS["json"](self.snap, self.results, Thresholds()))
        self.assertEqual(payload["account"]["id"], "act_1")
        self.assertEqual(payload["scope"]["adsets"], 7)
        self.assertTrue(payload["findings"])
        first = payload["findings"][0]
        self.assertEqual(first["severity"], "CRITICAL")
        self.assertIn("confidence", first)
        self.assertIn("thresholds", payload)

    def test_findings_are_ranked_by_severity_then_money(self):
        findings = collect(self.results)
        severities = [int(f.severity) for f in findings]
        self.assertEqual(severities, sorted(severities, reverse=True))
        for a, b in zip(findings, findings[1:]):
            if a.severity == b.severity:
                self.assertGreaterEqual(a.spend_at_stake, b.spend_at_stake)

    def test_text_report_explains_the_confidence_levels(self):
        text = RENDERERS["text"](self.snap, self.results, Thresholds())
        self.assertIn("HOW TO READ THIS", text)
        self.assertIn("confidence=structural", text)
        self.assertIn("confidence=heuristic", text)

    def test_skipped_checks_are_surfaced_as_their_own_section(self):
        snap = build_account()
        for ad in snap.ads:
            ad.creative["object_story_spec"]["link_data"]["link"] = ""
        text = RENDERERS["text"](snap, run_all(snap, Thresholds()), Thresholds())
        self.assertIn("SKIPPED CHECKS", text)
        self.assertIn("a skip is not a pass", text.lower())
        self.assertIn("tracking.utm", text)

    def test_currency_formatting_follows_the_account(self):
        krw = build_account()
        krw.currency = "KRW"
        text = RENDERERS["text"](krw, run_all(krw, Thresholds()), Thresholds())
        self.assertIn("KRW", text)
        self.assertNotIn("USD", text)


class TestCli(unittest.TestCase):
    def _snapshot_file(self, tmp: str) -> str:
        path = Path(tmp) / "snap.json"
        snapshot_io.save(build_account(), path)
        return str(path)

    def test_list_checks(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--list-checks"])
        self.assertEqual(code, 0)
        self.assertIn("learning.underbudgeted", buf.getvalue())

    def test_runs_offline_from_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--from-snapshot", self._snapshot_file(tmp)])
            self.assertEqual(code, 0)
            self.assertIn("META ADS ACCOUNT AUDIT", buf.getvalue())

    def test_writes_report_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "report.md"
            with redirect_stderr(io.StringIO()):
                code = main([
                    "--from-snapshot", self._snapshot_file(tmp),
                    "--format", "markdown", "--out", str(out),
                ])
            self.assertEqual(code, 0)
            self.assertIn("# Meta Ads audit", out.read_text(encoding="utf-8"))

    def test_only_filters_to_one_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                main([
                    "--from-snapshot", self._snapshot_file(tmp),
                    "--format", "json", "--only", "tracking.goal_mismatch",
                ])
            payload = json.loads(buf.getvalue())
            ids = {f["check_id"] for f in payload["findings"]}
            self.assertEqual(ids, {"tracking.goal_mismatch"})

    def test_unknown_check_id_is_rejected(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--only", "nope.nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown check id", err.getvalue())

    def test_fail_on_gates_the_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot_file(tmp)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--from-snapshot", snap]), 0)
                self.assertEqual(
                    main(["--from-snapshot", snap, "--fail-on", "critical"]), 1
                )

    def test_fail_on_stays_zero_when_nothing_reaches_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot_file(tmp)
            with redirect_stdout(io.StringIO()):
                code = main([
                    "--from-snapshot", snap,
                    "--only", "efficiency.undecidable",
                    "--fail-on", "high",
                ])
            self.assertEqual(code, 0)

    def test_missing_credentials_exit_with_a_usage_error(self):
        err = io.StringIO()
        with redirect_stderr(err), unittest.mock.patch.dict(
            "os.environ", {"META_ACCESS_TOKEN": "", "META_AD_ACCOUNT_ID": ""}, clear=False
        ):
            code = main(["--account", "act_1"])
        self.assertEqual(code, 2)
        self.assertIn("META_ACCESS_TOKEN", err.getvalue())

    def test_bad_snapshot_path_is_reported_cleanly(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--from-snapshot", "/nonexistent/snap.json"])
        self.assertEqual(code, 2)
        self.assertIn("could not read snapshot", err.getvalue())


import unittest.mock  # noqa: E402  (imported late, only the CLI test needs it)

if __name__ == "__main__":
    unittest.main()
