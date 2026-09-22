"""The setup check, driven by fake responses."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from metaaudit import preflight
from metaaudit.cli import main
from tests.test_api import FakeResponse, client_with

ACCOUNT_OK = {
    "id": "act_1",
    "name": "My Shop",
    "currency": "KRW",
    "timezone_name": "Asia/Seoul",
    "account_status": 1,
    "amount_spent": "5000000",
}


class TestPreflight(unittest.TestCase):
    def test_healthy_account_reports_ok_and_exits_zero(self):
        client, _, _ = client_with(
            [
                FakeResponse(200, ACCOUNT_OK),
                FakeResponse(200, {"data": [{"id": "c1"}]}),
            ]
        )
        code, report = preflight.run(client, "act_1")
        self.assertEqual(code, 0)
        self.assertIn("token is valid", report)
        self.assertIn("My Shop", report)
        self.assertIn("Asia/Seoul", report)
        self.assertIn("campaigns edge is readable", report)

    def test_krw_lifetime_spend_is_not_divided_by_a_hundred(self):
        client, _, _ = client_with(
            [FakeResponse(200, ACCOUNT_OK), FakeResponse(200, {"data": []})]
        )
        _, report = preflight.run(client, "act_1")
        self.assertIn("5,000,000 KRW", report)
        self.assertIn("minor unit divisor 1", report)

    def test_usd_lifetime_spend_is_divided_by_a_hundred(self):
        client, _, _ = client_with(
            [
                FakeResponse(200, {**ACCOUNT_OK, "currency": "USD"}),
                FakeResponse(200, {"data": []}),
            ]
        )
        _, report = preflight.run(client, "act_1")
        self.assertIn("50,000.00 USD", report)

    def test_probe_requests_exactly_what_the_audit_will(self):
        # Regression: preflight read a smaller field set than fetch, so it
        # reported a healthy setup and the audit then died on
        # "(#100) Requires business_management permission to access the field".
        from metaaudit.fetch import ACCOUNT_FIELDS

        client, session, _ = client_with(
            [FakeResponse(200, ACCOUNT_OK), FakeResponse(200, {"data": []})]
        )
        preflight.run(client, "act_1")
        probed = session.calls[0][1]["fields"].split(",")
        self.assertEqual(probed, list(ACCOUNT_FIELDS))

    def test_account_fields_need_no_permission_beyond_ads_read(self):
        from metaaudit.fetch import ACCOUNT_FIELDS

        # Fields Meta gates behind business_management or ads_management.
        gated = {"business", "owner", "funding_source_details", "users",
                 "agency_client_declaration", "extended_credit_invoice_group"}
        self.assertEqual(gated & set(ACCOUNT_FIELDS), set())

    def test_hint_is_runnable_on_this_platform(self):
        client, _, _ = client_with(
            [FakeResponse(200, ACCOUNT_OK), FakeResponse(200, {"data": []})]
        )
        code, report = preflight.run(client, "act_1", env_file=".env")
        self.assertEqual(code, 0)
        self.assertIn("--env-file .env", report)
        self.assertIn("py -m metaaudit" if os.name == "nt" else "python3 -m metaaudit", report)

    def test_bad_token_is_diagnosed_not_raised(self):
        client, _, _ = client_with(
            [FakeResponse(400, {"error": {"code": 190, "message": "expired"}})]
        )
        code, report = preflight.run(client, "act_1")
        self.assertEqual(code, 3)
        self.assertIn("FAILED to read the ad account", report)
        self.assertIn("System User tokens", report)

    def test_account_readable_but_edge_blocked_is_caught(self):
        # The failure mode a single account-node read would miss.
        client, _, _ = client_with(
            [
                FakeResponse(200, ACCOUNT_OK),
                FakeResponse(403, {"error": {"code": 200, "message": "no permission"}}),
            ]
        )
        code, report = preflight.run(client, "act_1")
        self.assertEqual(code, 3)
        self.assertIn("FAILED to list campaigns", report)
        self.assertIn("ads_read", report)

    def test_disabled_account_warns_but_still_passes(self):
        client, _, _ = client_with(
            [
                FakeResponse(200, {**ACCOUNT_OK, "account_status": 2, "disable_reason": 3}),
                FakeResponse(200, {"data": []}),
            ]
        )
        code, report = preflight.run(client, "act_1")
        self.assertEqual(code, 0)
        self.assertIn("DISABLED", report)
        self.assertIn("not delivering", report)

    def test_empty_account_is_reported_as_empty(self):
        client, _, _ = client_with(
            [FakeResponse(200, ACCOUNT_OK), FakeResponse(200, {"data": []})]
        )
        code, report = preflight.run(client, "act_1")
        self.assertEqual(code, 0)
        self.assertIn("no campaigns returned", report)


class TestCheckAuthFlag(unittest.TestCase):
    def test_conflicts_with_from_snapshot(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--check-auth", "--from-snapshot", "/tmp/x.json"])
        self.assertEqual(code, 2)
        self.assertIn("cannot run against a saved snapshot", err.getvalue())

    def test_requires_credentials(self):
        err = io.StringIO()
        with unittest.mock.patch.dict(
            "os.environ", {"META_ACCESS_TOKEN": "", "META_AD_ACCOUNT_ID": ""}, clear=False
        ), redirect_stderr(err):
            code = main(["--check-auth", "--account", "act_1"])
        self.assertEqual(code, 2)
        self.assertIn("META_ACCESS_TOKEN", err.getvalue())


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
