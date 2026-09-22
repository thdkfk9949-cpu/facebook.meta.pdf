"""Turn structural findings into a reviewable list of account changes.

Nothing here talks to the API. A plan is a proposal: it is built, printed,
read by a human, and only then handed to :mod:`metaaudit.apply`.

Two rules decide what may enter a plan, and both exist because this tool's
whole argument is that most wasted ad spend comes from acting on evidence
that cannot carry the decision:

1. **Structural findings only.** A finding is eligible when it follows from
   configuration arithmetic (``Confidence.STRUCTURAL``) — true regardless of
   how little the account has spent. Anything resting on this account's
   performance numbers is never auto-applied, however obvious it looks.
2. **Free changes by default.** Consolidating budget into fewer ad sets costs
   nothing, so it is proposed outright. Raising a budget spends more money
   every day it runs, so it needs an explicit allowance with a number on it.

Findings that are structural but need a decision this tool cannot make —
which pixel, which conversion event — become ``manual`` entries. They are
reported, never silently dropped: a skipped fix is not a fixed one.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .checks.base import CheckResult, Confidence, Finding
from .currency import fmt, to_minor
from .fetch import AdSet, Campaign, Snapshot

SCHEMA_VERSION = 1

# Ad set fields this module is willing to write. Anything outside this set is
# a manual item by construction, so a new check cannot silently gain the power
# to mutate an account.
WRITABLE_FIELDS = frozenset({"daily_budget", "status"})


@dataclass
class Change:
    """One field write, with everything needed to review and reverse it."""

    kind: str                 # "pause" | "budget"
    level: str                # "adset" | "campaign"
    entity_id: str
    entity_name: str
    field_name: str           # the Graph API field being written
    before: Any               # current value, in the units the API uses
    after: Any
    before_display: str       # the same values for a human
    after_display: str
    reason: str
    source_check: str
    # Change in daily spend this causes, major units. Negative frees money.
    daily_delta: float = 0.0

    def api_params(self) -> dict[str, Any]:
        return {self.field_name: self.after}


@dataclass
class ManualItem:
    """A structural finding that needs a human decision before it can be made."""

    entity_id: str
    entity_name: str
    level: str
    source_check: str
    what: str
    why_manual: str


@dataclass
class Plan:
    account_id: str
    currency: str
    generated_at: str
    window: str
    changes: list[Change] = field(default_factory=list)
    manual: list[ManualItem] = field(default_factory=list)
    budget_allowance: float = 0.0

    @property
    def daily_delta(self) -> float:
        return sum(c.daily_delta for c in self.changes)

    @property
    def is_empty(self) -> bool:
        return not self.changes


# --- building ---------------------------------------------------------------


def _structural(results: Iterable[CheckResult], check_id: str) -> list[Finding]:
    for result in results:
        if result.check_id == check_id:
            return [
                f for f in result.findings if f.confidence is Confidence.STRUCTURAL
            ]
    return []


def _by_id(snap: Snapshot) -> tuple[dict[str, AdSet], dict[str, Campaign]]:
    adsets = {a.id: a for a in snap.adsets}
    campaigns = {c.id: c for c in snap.campaigns}
    return adsets, campaigns


def _campaign_of(adset: AdSet, campaigns: dict[str, Campaign]) -> Campaign | None:
    return campaigns.get(adset.campaign_id)


def _is_cbo(adset: AdSet, campaigns: dict[str, Campaign]) -> bool:
    """True when the campaign holds the budget, so ad set budgets are not ours.

    Under campaign budget optimisation Meta redistributes across ad sets by
    itself. Pausing still works and still concentrates the budget; writing an
    ad set budget does not, and would be rejected or silently ignored.
    """
    campaign = _campaign_of(adset, campaigns)
    if campaign is None:
        return False
    return adset.daily_budget <= 0 and (
        campaign.daily_budget > 0 or campaign.lifetime_budget > 0
    )


def _pause(adset: AdSet, reason: str, check_id: str, currency: str) -> Change:
    return Change(
        kind="pause",
        level="adset",
        entity_id=adset.id,
        entity_name=adset.name,
        field_name="status",
        before=adset.status or "ACTIVE",
        after="PAUSED",
        before_display=adset.status or "ACTIVE",
        after_display="PAUSED",
        reason=reason,
        source_check=check_id,
        daily_delta=-adset.daily_budget,
    )


def _set_budget(
    adset: AdSet, new_daily: float, reason: str, check_id: str, currency: str
) -> Change:
    return Change(
        kind="budget",
        level="adset",
        entity_id=adset.id,
        entity_name=adset.name,
        field_name="daily_budget",
        before=to_minor(adset.daily_budget, currency),
        after=to_minor(new_daily, currency),
        before_display=fmt(adset.daily_budget, currency),
        after_display=fmt(new_daily, currency),
        reason=reason,
        source_check=check_id,
        daily_delta=new_daily - adset.daily_budget,
    )


def _rank_for_survival(adset: AdSet) -> tuple:
    """Which of several interchangeable ad sets to keep.

    Conversions first, then spend: the survivor should be the one Meta has
    already learned the most about. This is a tie-break among ad sets the
    audit has already judged structurally redundant — it is not a performance
    claim, and nothing here is reported as one.
    """
    conversions = adset.insights.conversions(adset.conversion_action_types())
    return (conversions, adset.insights.spend, adset.id)


def _consolidate(
    keep: AdSet,
    drop: list[AdSet],
    reason: str,
    check_id: str,
    currency: str,
    campaigns: dict[str, Campaign],
) -> list[Change]:
    """Pause the redundant ad sets and move their budget onto the survivor.

    Net daily spend is unchanged: this is the free fix. Under CBO there is no
    ad set budget to move, so only the pauses are emitted and Meta reallocates.
    """
    changes = [_pause(a, reason, check_id, currency) for a in drop]
    if _is_cbo(keep, campaigns):
        return changes
    freed = sum(a.daily_budget for a in drop)
    if freed > 0 and keep.daily_budget > 0:
        changes.append(
            _set_budget(
                keep,
                keep.daily_budget + freed,
                f"Absorbs the budget freed by pausing {len(drop)} redundant "
                f"ad set(s), so campaign spend is unchanged.",
                check_id,
                currency,
            )
        )
    return changes


def build_plan(
    snap: Snapshot,
    results: list[CheckResult],
    *,
    budget_allowance: float = 0.0,
    now: dt.datetime | None = None,
) -> Plan:
    """Build the change set. ``budget_allowance`` is major units per day."""
    adsets, campaigns = _by_id(snap)
    plan = Plan(
        account_id=snap.account_id,
        currency=snap.currency,
        generated_at=(now or dt.datetime.now(dt.timezone.utc)).isoformat(
            timespec="seconds"
        ),
        window=f"{snap.since}..{snap.until}",
        budget_allowance=budget_allowance,
    )
    touched: set[str] = set()

    # --- identical targeting: keep one, pause the rest ----------------------
    for finding in _structural(results, "structure.self_competition"):
        if finding.evidence.get("tier") != "identical":
            plan.manual.append(
                ManualItem(
                    entity_id=finding.entity_id,
                    entity_name=finding.entity_name,
                    level=finding.level,
                    source_check=finding.check_id,
                    what=finding.recommendation,
                    why_manual=(
                        "The ad sets share a demographic footprint but target "
                        "different audiences. Whether they truly overlap needs "
                        "the Audience Overlap tool, which the API does not expose."
                    ),
                )
            )
            continue
        group = [
            adsets[d["id"]]
            for d in finding.evidence.get("adsets", [])
            if d.get("id") in adsets and adsets[d["id"]].is_delivering
        ]
        group = [a for a in group if a.id not in touched]
        if len(group) < 2:
            continue
        group.sort(key=_rank_for_survival, reverse=True)
        keep, drop = group[0], group[1:]
        plan.changes.extend(
            _consolidate(
                keep,
                drop,
                f"Identical targeting to {keep.name}: these ad sets bid against "
                f"each other for the same people and split the events each one "
                f"needs to leave the learning phase.",
                finding.check_id,
                snap.currency,
                campaigns,
            )
        )
        touched.update(a.id for a in group)

    # --- fragmentation: cut the ad set count to what the budget supports ----
    for finding in _structural(results, "structure.fragmentation"):
        campaign = campaigns.get(finding.entity_id)
        if campaign is None:
            continue
        active = [a for a in campaign.adsets if a.is_delivering and a.id not in touched]
        supportable = max(1, int(finding.evidence.get("supportable_adsets", 1) or 1))
        if len(active) <= supportable:
            continue
        active.sort(key=_rank_for_survival, reverse=True)
        keep, drop = active[0], active[supportable:]
        if not drop:
            continue
        plan.changes.extend(
            _consolidate(
                keep,
                drop,
                f"Campaign budget supports {supportable} ad set(s) at this CPA "
                f"but is split across {len(active)}, so every one of them is "
                f"starved of the 50 weekly events it needs.",
                finding.check_id,
                snap.currency,
                campaigns,
            )
        )
        touched.update(a.id for a in drop)
        touched.add(keep.id)

    # --- underbudgeted: costs money, so it needs an explicit allowance ------
    spent_allowance = 0.0
    for finding in _structural(results, "learning.underbudgeted"):
        adset = adsets.get(finding.entity_id)
        if adset is None or adset.id in touched or not adset.is_delivering:
            continue
        required = float(finding.evidence.get("required_daily_budget", 0) or 0)
        current = float(finding.evidence.get("daily_budget", 0) or 0)
        increase = required - current
        if required <= 0 or increase <= 0:
            continue
        if _is_cbo(adset, campaigns):
            plan.manual.append(
                ManualItem(
                    entity_id=adset.id,
                    entity_name=adset.name,
                    level="adset",
                    source_check=finding.check_id,
                    what=finding.recommendation,
                    why_manual=(
                        "The campaign holds the budget (CBO), so the fix is a "
                        "campaign-level budget decision, not an ad set one."
                    ),
                )
            )
            continue
        if spent_allowance + increase > budget_allowance:
            plan.manual.append(
                ManualItem(
                    entity_id=adset.id,
                    entity_name=adset.name,
                    level="adset",
                    source_check=finding.check_id,
                    what=(
                        f"Raise daily budget from {fmt(current, snap.currency)} to "
                        f"{fmt(required, snap.currency)} "
                        f"(+{fmt(increase, snap.currency)}/day)."
                    ),
                    why_manual=(
                        "This increases daily spend. Re-run with "
                        f"--allow-budget-increase {increase:.0f} to include it, "
                        "or consolidate instead — that costs nothing."
                    ),
                )
            )
            continue
        spent_allowance += increase
        plan.changes.append(
            _set_budget(
                adset,
                required,
                f"At a CPA of {fmt(float(finding.evidence.get('cpa', 0) or 0), snap.currency)} "
                f"this ad set needs {fmt(required, snap.currency)}/day to reach 50 "
                f"events a week. Below that it cannot leave the learning phase, "
                f"whatever the creative does.",
                finding.check_id,
                snap.currency,
            )
        )
        touched.add(adset.id)

    # --- structural, but not ours to decide ---------------------------------
    for check_id, why in (
        (
            "tracking.goal_mismatch",
            "Switching the optimisation goal needs a pixel and a conversion "
            "event chosen for it; the audit cannot pick those for you.",
        ),
        (
            "tracking.missing_pixel",
            "Which pixel and event this ad set should optimise for is a "
            "measurement decision, not an arithmetic one.",
        ),
    ):
        for finding in _structural(results, check_id):
            plan.manual.append(
                ManualItem(
                    entity_id=finding.entity_id,
                    entity_name=finding.entity_name,
                    level=finding.level,
                    source_check=check_id,
                    what=finding.recommendation,
                    why_manual=why,
                )
            )

    return plan


# --- serialisation ----------------------------------------------------------


def dumps(plan: Plan) -> str:
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, "plan": asdict(plan)},
        indent=2,
        ensure_ascii=False,
    )


def loads(text: str) -> Plan:
    payload = json.loads(text)
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"plan schema v{version} is not readable by this version "
            f"(expected v{SCHEMA_VERSION}); regenerate it with --plan"
        )
    raw = payload["plan"]
    changes = [Change(**c) for c in raw.get("changes", [])]
    for change in changes:
        if change.field_name not in WRITABLE_FIELDS:
            raise ValueError(
                f"plan contains a write to {change.field_name!r}, which this "
                f"version refuses to apply"
            )
    return Plan(
        account_id=raw["account_id"],
        currency=raw["currency"],
        generated_at=raw["generated_at"],
        window=raw.get("window", ""),
        changes=changes,
        manual=[ManualItem(**m) for m in raw.get("manual", [])],
        budget_allowance=raw.get("budget_allowance", 0.0),
    )


# --- rendering --------------------------------------------------------------


def render(plan: Plan) -> str:
    """The text a human reads before deciding whether to apply any of this."""
    cur = plan.currency
    lines: list[str] = []
    bar = "=" * 88
    lines.append(bar)
    lines.append(f"CHANGE PLAN  —  {plan.account_id}")
    lines.append(bar)
    lines.append(f"Built from : {plan.window}  ({plan.generated_at})")
    lines.append(f"Changes    : {len(plan.changes)}   Needs a decision: {len(plan.manual)}")
    delta = plan.daily_delta
    if abs(delta) < 0.005:
        lines.append("Daily spend: unchanged — this plan only moves money, it does not add any")
    else:
        sign = "+" if delta > 0 else ""
        lines.append(f"Daily spend: {sign}{fmt(delta, cur)}/day")
    lines.append("")

    if not plan.changes:
        lines.append("No change is safe to make automatically from this audit.")
    for i, change in enumerate(plan.changes, 1):
        lines.append("-" * 88)
        lines.append(
            f"{i}. [{change.kind}] {change.entity_name}  ({change.level} {change.entity_id})"
        )
        lines.append(
            f"   {change.field_name}: {change.before_display}  ->  {change.after_display}"
        )
        lines.append(f"   from={change.source_check}")
        lines.append("")
        for line in _wrap(change.reason, 84):
            lines.append(f"   {line}")
        lines.append("")

    if plan.manual:
        lines.append("=" * 88)
        lines.append("NOT APPLIED — these need a decision this tool cannot make for you")
        lines.append("=" * 88)
        for item in plan.manual:
            lines.append("")
            lines.append(f"* {item.entity_name}  ({item.level} {item.entity_id})")
            lines.append(f"  from={item.source_check}")
            for line in _wrap(item.what, 84):
                lines.append(f"  {line}")
            lines.append("")
            for line in _wrap(f"Why not automatic: {item.why_manual}", 84):
                lines.append(f"  {line}")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines
