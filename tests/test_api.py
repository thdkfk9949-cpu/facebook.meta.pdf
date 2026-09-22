"""Graph client behaviour, driven by a fake session — no network."""

from __future__ import annotations

import json
import unittest

import requests

from metaaudit.api import GraphClient, GraphError


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text or json.dumps(payload or {})
        self.url = "https://graph.facebook.com/v23.0/act_1?access_token=SECRET"

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not self.responses:
            raise AssertionError("more requests than queued responses")
        return self.responses.pop(0)


def client_with(responses, **kw):
    session = FakeSession(responses)
    slept: list[float] = []
    client = GraphClient(
        "SECRET",
        "v23.0",
        session=session,
        sleep=slept.append,
        **kw,
    )
    return client, session, slept


class TestRedaction(unittest.TestCase):
    def test_tokens_never_survive_into_messages(self):
        client, _, _ = client_with([])
        url = (
            "https://graph.facebook.com/v23.0/act_1"
            "?access_token=SECRET&fields=name&appsecret_proof=PROOF"
        )
        redacted = client._redact(url)
        self.assertNotIn("SECRET", redacted)
        self.assertNotIn("PROOF", redacted)
        self.assertIn("fields=name", redacted)

    def test_redaction_terminates_and_is_idempotent(self):
        # Regression: a naive replace loop never terminates, because the
        # replacement still contains the key it searches for.
        client, _, _ = client_with([])
        once = client._redact("https://x/?access_token=A")
        self.assertEqual(client._redact(once), once)


class TestTransportErrorRedaction(unittest.TestCase):
    """Regression: a connection failure leaked the token.

    ``requests`` builds its exception message from the *prepared* URL, which
    carries the query string, so the token rode out inside the message even
    though the url argument was redacted.
    """

    def test_connection_error_message_is_redacted(self):
        real_url = (
            "https://graph.facebook.com/v23.0/act_1"
            "?fields=name&access_token=SECRET&appsecret_proof=PROOF"
        )

        class ExplodingSession:
            def get(self, url, params=None, timeout=None):
                raise requests.ConnectionError(
                    f"Max retries exceeded with url: {real_url} "
                    "(Caused by ProxyError('Unable to connect to proxy'))"
                )

        client = GraphClient(
            "SECRET",
            "v23.0",
            app_secret="s3cr3t",
            session=ExplodingSession(),
            max_retries=1,
            sleep=lambda _: None,
        )
        with self.assertRaises(GraphError) as ctx:
            client.get("act_1", {"fields": "name"})

        rendered = str(ctx.exception)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("PROOF", rendered)
        self.assertNotIn("SECRET", ctx.exception.message)
        # The diagnosis must survive the redaction.
        self.assertIn("Unable to connect to proxy", rendered)
        self.assertIn("fields=name", rendered)

    def test_error_body_echoing_a_token_is_redacted(self):
        # Non-JSON error bodies are passed through verbatim; if one echoes the
        # request, it must not carry the token with it.
        body = "Bad Request: /v23.0/act_1?access_token=SECRET"
        client, _, _ = client_with([FakeResponse(400, None, text=body)])
        with self.assertRaises(GraphError) as ctx:
            client.get("act_1")
        self.assertNotIn("SECRET", str(ctx.exception))


class TestAppSecretProof(unittest.TestCase):
    def test_proof_added_only_when_secret_present(self):
        with_secret, _, _ = client_with([], app_secret="s3cr3t")
        self.assertIn("appsecret_proof", with_secret._auth_params())
        without, _, _ = client_with([])
        self.assertNotIn("appsecret_proof", without._auth_params())


class TestErrors(unittest.TestCase):
    def test_expired_token_gets_an_actionable_hint(self):
        client, _, _ = client_with(
            [FakeResponse(400, {"error": {"code": 190, "message": "expired"}})]
        )
        with self.assertRaises(GraphError) as ctx:
            client.get("act_1")
        self.assertEqual(ctx.exception.code, 190)
        self.assertIn("expired", str(ctx.exception))
        self.assertIn("System User tokens", str(ctx.exception))

    def test_permanent_error_is_not_retried(self):
        client, session, _ = client_with(
            [FakeResponse(400, {"error": {"code": 100, "error_subcode": 33}})]
        )
        with self.assertRaises(GraphError):
            client.get("act_1")
        self.assertEqual(len(session.calls), 1)

    def test_rate_limit_is_retried_then_succeeds(self):
        client, session, slept = client_with(
            [
                FakeResponse(400, {"error": {"code": 80004, "message": "too many"}}),
                FakeResponse(200, {"data": [{"id": "1"}]}),
            ]
        )
        payload = client.get("act_1")
        self.assertEqual(payload["data"][0]["id"], "1")
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(slept, "a rate-limited retry must back off before retrying")

    def test_server_error_is_retried(self):
        client, session, _ = client_with(
            [FakeResponse(500, {"error": {"message": "boom"}}), FakeResponse(200, {"data": []})]
        )
        client.get("act_1")
        self.assertEqual(len(session.calls), 2)


class TestRateHeaders(unittest.TestCase):
    def test_high_usage_triggers_a_pause(self):
        header = json.dumps(
            {"123": [{"call_count": 95, "total_cputime": 20, "total_time": 30}]}
        )
        client, _, slept = client_with(
            [FakeResponse(200, {"data": []}, {"X-Business-Use-Case-Usage": header})]
        )
        client.get("act_1")
        self.assertEqual(client.rate.call_count_pct, 95)
        self.assertIn(30, slept)

    def test_malformed_header_is_ignored(self):
        client, _, slept = client_with(
            [FakeResponse(200, {"data": []}, {"X-Business-Use-Case-Usage": "not json"})]
        )
        client.get("act_1")
        self.assertEqual(client.rate.worst_pct, 0.0)
        self.assertEqual(slept, [])


class TestPagination(unittest.TestCase):
    def test_follows_cursors_and_resigns_each_hop(self):
        page1 = FakeResponse(
            200,
            {
                "data": [{"id": "1"}, {"id": "2"}],
                "paging": {"cursors": {"after": "CUR1"}, "next": "https://next"},
            },
        )
        page2 = FakeResponse(200, {"data": [{"id": "3"}], "paging": {}})
        client, session, _ = client_with([page1, page2])
        ids = [n["id"] for n in client.paginate("act_1/ads")]
        self.assertEqual(ids, ["1", "2", "3"])
        # Second hop carries our own token, not one lifted from the next URL.
        self.assertEqual(session.calls[1][1]["after"], "CUR1")
        self.assertEqual(session.calls[1][1]["access_token"], "SECRET")

    def test_stops_when_next_is_absent(self):
        client, session, _ = client_with(
            [FakeResponse(200, {"data": [{"id": "1"}], "paging": {"cursors": {"after": "C"}}})]
        )
        self.assertEqual(len(list(client.paginate("act_1/ads"))), 1)
        self.assertEqual(len(session.calls), 1)

    def test_respects_max_pages_guard(self):
        endless = [
            FakeResponse(
                200,
                {
                    "data": [{"id": "x"}],
                    "paging": {"cursors": {"after": "C"}, "next": "https://n"},
                },
            )
            for _ in range(10)
        ]
        client, session, _ = client_with(endless)
        nodes = list(client.paginate("act_1/ads", max_pages=3))
        self.assertEqual(len(nodes), 3)
        self.assertEqual(len(session.calls), 3)


if __name__ == "__main__":
    unittest.main()
