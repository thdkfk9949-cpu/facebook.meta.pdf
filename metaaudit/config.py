"""Configuration and the thresholds every check is judged against.

Thresholds live here, in one place, with the reasoning attached. They are
defaults, not laws: an account with a 30-day consideration cycle needs
different frequency limits than an impulse-buy account. Override with a JSON
file via ``--thresholds``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Meta's own guidance: an ad set needs roughly this many optimization events
# per 7 days to leave the learning phase and get stable delivery. Everything
# in the budget math follows from this number.
EVENTS_TO_EXIT_LEARNING = 50
LEARNING_WINDOW_DAYS = 7


@dataclass
class Thresholds:
    # --- learning phase / budget math ---
    events_to_exit_learning: int = EVENTS_TO_EXIT_LEARNING
    # An ad set budgeted below this fraction of the budget its own CPA implies
    # is structurally unable to reach 50 events/week. Not "underperforming" —
    # arithmetically unable.
    underbudget_ratio: float = 0.8
    # Significant edits restart learning. Ad sets edited this recently are
    # still settling, so their numbers are not yet decision-grade.
    recent_edit_days: int = 3

    # --- fatigue ---
    # 7-day frequency above this on cold traffic means the same people are
    # seeing the same thing repeatedly.
    frequency_warn: float = 3.0
    frequency_high: float = 4.5
    # Relative CTR drop vs the previous equal-length window that, combined
    # with high frequency, confirms fatigue rather than noise.
    ctr_decline_pct: float = 20.0

    # --- creative diversity ---
    # Ad sets delivering with fewer active ads than this give the algorithm
    # almost nothing to optimize across.
    min_active_ads_per_adset: int = 3
    # Distinct primary texts / images below this share means the "different"
    # ads are near-duplicates.
    min_distinct_creative_ratio: float = 0.6

    # --- statistical guardrails ---
    # Below this many conversions in the window, no performance claim about
    # the entity is made at all. Reporting a CPA off 3 conversions is noise
    # laundered as insight.
    min_conversions_for_claim: int = 30
    # Two-sided alpha for every comparison test.
    alpha: float = 0.05
    # Events needed at the top of a funnel transition before its rate is
    # reported. Below this the rate has no useful upper bound, so "1 of 3
    # people dropped out" is not a finding, it is an anecdote.
    funnel_min_upstream: int = 30
    # A transition that keeps less than this share of the people who reached
    # it is where the funnel is actually breaking, not merely narrowing.
    funnel_collapse_ratio: float = 0.5

    # --- spend floors ---
    # Entities below this share of account spend are not worth a finding;
    # they are rounding error and they crowd out the report.
    min_spend_share_to_report: float = 0.01

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None) -> "Thresholds":
        if not path:
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(
                f"unknown threshold keys in {path}: {', '.join(sorted(unknown))}"
            )
        return cls(**raw)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class Settings:
    access_token: str
    ad_account_id: str
    api_version: str = "v23.0"
    app_secret: str | None = None
    window_days: int = 30
    thresholds: Thresholds = field(default_factory=Thresholds)

    @staticmethod
    def normalize_account_id(raw: str) -> str:
        """Accept ``123``, ``act_123`` or a full path; always return ``act_123``."""
        value = (raw or "").strip().rstrip("/")
        value = value.rsplit("/", 1)[-1]
        if not value:
            raise ValueError("ad account id is empty")
        if value.startswith("act_"):
            value = value[4:]
        if not value.isdigit():
            raise ValueError(
                f"ad account id must be numeric (optionally act_-prefixed), got {raw!r}"
            )
        return f"act_{value}"

    @classmethod
    def from_env(
        cls,
        *,
        account: str | None = None,
        api_version: str | None = None,
        window_days: int = 30,
        thresholds: Thresholds | None = None,
        env: dict[str, str] | None = None,
    ) -> "Settings":
        src = env if env is not None else os.environ
        token = (src.get("META_ACCESS_TOKEN") or "").strip()
        if not token:
            raise SystemExit(
                "META_ACCESS_TOKEN is not set. Copy .env.example to .env, fill it "
                "in, and either export it or run with --env-file .env"
            )
        raw_account = account or src.get("META_AD_ACCOUNT_ID") or ""
        if not raw_account.strip():
            raise SystemExit(
                "No ad account. Pass --account act_123... or set META_AD_ACCOUNT_ID"
            )
        return cls(
            access_token=token,
            ad_account_id=cls.normalize_account_id(raw_account),
            api_version=(api_version or src.get("META_API_VERSION") or "v23.0").strip(),
            app_secret=(src.get("META_APP_SECRET") or "").strip() or None,
            window_days=window_days,
            thresholds=thresholds or Thresholds(),
        )


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Minimal ``.env`` reader — no dependency on python-dotenv.

    Handles ``KEY=value``, ``export KEY=value``, ``#`` comments, and single or
    double quoted values. Does not handle interpolation, by design: a token
    containing ``$`` should survive unchanged.
    """
    out: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            raise ValueError(f"{path}:{lineno}: expected KEY=value, got {line!r}")
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out
