"""fetch_snapshot against a fake Graph client: what it asks for and what it warns."""

from __future__ import annotations

import datetime as dt
import json
import unittest

from metaaudit.fetch import fetch_snapshot


class FakeClient:
    """Answers with no campaigns, but account-level spend in the window."""

    def __init__(self, spend="237974"):
        self.spend = spend
        self.request_count = 0
        self.filters: list[list[str]] = []

    def get(self, path, params=None):
        self.request_count += 1
        return {"id": path, "name": "Test", "currency": "KRW"}

    def paginate(self, path, params=None):
        self.request_count += 1
        params = params or {}
        if "filtering" in params:
            self.filters.append(json.loads(params["filtering"])[0]["value"])
        if path.endswith("/insights") and params.get("level") == "account":
            return iter([{"spend": self.spend, "impressions": "1000"}])
        return iter([])


TODAY = dt.date(2026, 11, 3)


class TestFetchScope(unittest.TestCase):
    def test_include_paused_also_reaches_children_of_paused_parents(self):
        client = FakeClient()
        fetch_snapshot(client, "act_1", include_paused=True, today=TODAY)
        for statuses in client.filters:
            self.assertIn("CAMPAIGN_PAUSED", statuses)
            self.assertIn("ADSET_PAUSED", statuses)

    def test_default_is_active_only(self):
        client = FakeClient()
        fetch_snapshot(client, "act_1", today=TODAY)
        self.assertTrue(client.filters)
        self.assertTrue(all(f == ["ACTIVE"] for f in client.filters))

    def test_spend_with_nothing_active_points_at_include_paused(self):
        snap = fetch_snapshot(FakeClient(), "act_1", today=TODAY)
        self.assertTrue(any("--include-paused" in w for w in snap.warnings))

    def test_no_warning_when_nothing_was_spent(self):
        snap = fetch_snapshot(FakeClient(spend="0"), "act_1", today=TODAY)
        self.assertEqual(snap.warnings, [])


if __name__ == "__main__":
    unittest.main()
