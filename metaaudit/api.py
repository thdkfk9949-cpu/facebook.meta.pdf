"""Graph API client for the Marketing API.

Deliberately built on ``requests`` alone rather than the facebook-business
SDK: the SDK pins its own Graph version and turns a version bump into a
dependency upgrade, and it hides the rate-limit headers this client needs to
read. Everything here is read-only — no POST, no DELETE.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

log = logging.getLogger("metaaudit.api")

GRAPH_HOST = "https://graph.facebook.com"

# Errors that mean "slow down", not "you did it wrong".
RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80003, 80004, 80005, 80006, 80008}
# Transient server-side faults worth one more attempt.
TRANSIENT_CODES = {1, 2}


class GraphError(RuntimeError):
    """A Graph API error with the bits that actually help you fix it."""

    def __init__(self, status: int, payload: dict[str, Any], url: str):
        self.status = status
        self.payload = payload
        self.url = url
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        self.type = err.get("type")
        self.message = err.get("message") or f"HTTP {status}"
        self.fbtrace_id = err.get("fbtrace_id")
        super().__init__(self._render())

    def _render(self) -> str:
        parts = [f"Graph API error {self.status}: {self.message}"]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        if self.fbtrace_id:
            parts.append(f"fbtrace_id={self.fbtrace_id}")
        body = " | ".join(parts)
        return f"{body}\n{self.hint()}" if self.hint() else body

    def hint(self) -> str:
        """Map the common failures to the thing you actually have to change."""
        if self.code == 190:
            return (
                "Hint: the access token is invalid or expired. System User tokens "
                "do not expire unless revoked; a token copied from Graph API "
                "Explorer expires in about an hour."
            )
        if self.code == 200 or self.code == 10:
            return (
                "Hint: the token lacks a required permission. The audit needs "
                "ads_read, and the token's user needs a role on this ad account."
            )
        if self.code == 100 and self.subcode == 33:
            return (
                "Hint: the ad account id is wrong, or this token has no access "
                "to it. Check META_AD_ACCOUNT_ID."
            )
        if self.code == 2635 or "Unsupported get request" in (self.message or ""):
            return (
                "Hint: this Graph API version may be deprecated. Bump "
                "META_API_VERSION or pass --api-version."
            )
        if self.code in RATE_LIMIT_CODES:
            return (
                "Hint: rate limited. The client already backs off; if this "
                "persists, re-run with a shorter --window or fewer entities."
            )
        return ""

    @property
    def is_rate_limit(self) -> bool:
        return self.code in RATE_LIMIT_CODES

    @property
    def is_retryable(self) -> bool:
        return self.is_rate_limit or self.code in TRANSIENT_CODES or self.status >= 500


@dataclass
class RateLimitState:
    """Last-seen values from Meta's throttling headers."""

    call_count_pct: float = 0.0
    total_cputime_pct: float = 0.0
    total_time_pct: float = 0.0
    estimated_time_to_regain_access: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def worst_pct(self) -> float:
        return max(self.call_count_pct, self.total_cputime_pct, self.total_time_pct)


class GraphClient:
    def __init__(
        self,
        access_token: str,
        api_version: str = "v23.0",
        *,
        app_secret: str | None = None,
        session: requests.Session | None = None,
        max_retries: int = 5,
        timeout: float = 60.0,
        sleep: Any = time.sleep,
    ):
        if not access_token:
            raise ValueError("access_token is required")
        self.access_token = access_token
        self.api_version = api_version if api_version.startswith("v") else f"v{api_version}"
        self.app_secret = app_secret
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep
        self.rate = RateLimitState()
        self.request_count = 0

    # -- auth ---------------------------------------------------------------

    def _auth_params(self) -> dict[str, str]:
        params = {"access_token": self.access_token}
        if self.app_secret:
            # Required when the app has "Require app secret" enabled.
            params["appsecret_proof"] = hmac.new(
                self.app_secret.encode("utf-8"),
                self.access_token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return params

    _SECRET_RE = re.compile(
        r"(access_token|appsecret_proof)=[^&#\s]*", re.IGNORECASE
    )

    @classmethod
    def _redact(cls, url: str) -> str:
        """Never let a token reach a log line, a traceback, or a report."""
        return cls._SECRET_RE.sub(r"\1=REDACTED", url)

    # -- throttling ---------------------------------------------------------

    def _absorb_rate_headers(self, resp: requests.Response) -> None:
        raw = resp.headers.get("X-Business-Use-Case-Usage") or resp.headers.get(
            "X-Ad-Account-Usage"
        )
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return
        entries: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            for value in parsed.values():
                if isinstance(value, list):
                    entries.extend(x for x in value if isinstance(x, dict))
                elif isinstance(value, dict):
                    entries.append(value)
        if not entries:
            return
        self.rate = RateLimitState(
            call_count_pct=max(float(e.get("call_count", 0) or 0) for e in entries),
            total_cputime_pct=max(float(e.get("total_cputime", 0) or 0) for e in entries),
            total_time_pct=max(float(e.get("total_time", 0) or 0) for e in entries),
            estimated_time_to_regain_access=max(
                int(e.get("estimated_time_to_regain_access", 0) or 0) for e in entries
            ),
            raw=parsed if isinstance(parsed, dict) else {},
        )
        # Ease off before Meta cuts us off rather than after.
        if self.rate.worst_pct >= 90:
            log.warning(
                "rate limit at %.0f%% of quota — pausing 30s", self.rate.worst_pct
            )
            self._sleep(30)
        elif self.rate.worst_pct >= 75:
            self._sleep(3)

    def _backoff(self, attempt: int, err: GraphError | None) -> float:
        if err is not None and err.is_rate_limit:
            wait = self.rate.estimated_time_to_regain_access
            if wait > 0:
                return min(float(wait) * 60.0, 600.0)
            return min(60.0 * (2**attempt), 600.0)
        return min(2.0**attempt, 32.0) + random.uniform(0, 1.0)

    # -- requests -----------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{GRAPH_HOST}/{self.api_version}/{path.lstrip('/')}"
        return self._request(url, {**(params or {}), **self._auth_params()})

    def _request(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        last: GraphError | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.max_retries - 1:
                    raise GraphError(0, {"error": {"message": str(exc)}}, self._redact(url))
                self._sleep(self._backoff(attempt, None))
                continue

            self.request_count += 1
            self._absorb_rate_headers(resp)

            if resp.ok:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise GraphError(
                        resp.status_code,
                        {"error": {"message": f"non-JSON response: {exc}"}},
                        self._redact(resp.url),
                    ) from exc

            try:
                payload = resp.json()
            except ValueError:
                payload = {"error": {"message": resp.text[:500]}}
            err = GraphError(resp.status_code, payload, self._redact(resp.url))
            if not err.is_retryable or attempt == self.max_retries - 1:
                raise err
            delay = self._backoff(attempt, err)
            log.warning(
                "retrying after %.1fs (attempt %d/%d): %s",
                delay, attempt + 1, self.max_retries, err.message,
            )
            self._sleep(delay)
            last = err
        raise last or GraphError(0, {"error": {"message": "exhausted retries"}}, url)

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int = 200,
    ) -> Iterator[dict[str, Any]]:
        """Yield every node across cursor-paged edges.

        Follows ``paging.next`` but re-signs each hop with our own credentials
        instead of trusting the token embedded in the returned URL.
        """
        page_params = {**(params or {})}
        page_params.setdefault("limit", 200)
        current_path = path
        for _ in range(max_pages):
            payload = self.get(current_path, page_params)
            for node in payload.get("data", []) or []:
                yield node
            after = (payload.get("paging", {}) or {}).get("cursors", {}).get("after")
            next_url = (payload.get("paging", {}) or {}).get("next")
            if not next_url or not after:
                return
            page_params["after"] = after
        log.warning("stopped paginating %s after %d pages", path, max_pages)
