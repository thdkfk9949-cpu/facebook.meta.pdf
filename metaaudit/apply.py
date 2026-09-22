"""Apply a reviewed plan, one field at a time, and record how to undo it.

Three properties matter more than speed here, because the thing being edited
spends money:

**Read before write.** Every change carries the value it expected to find.
Before writing, the current value is fetched and compared. If the account has
moved since the plan was built — someone edited the ad set, Meta changed a
status — the change is skipped and reported, never forced. A plan is a
statement about a known state, and it stops being valid when that state does.

**Rollback first.** The undo file is written to disk *before* the first API
call, not after the last one. A run interrupted halfway still leaves a
complete record of what it was about to do.

**Partial failure is reported, not swallowed.** Each change succeeds or fails
on its own. The summary names every skip and every error.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .api import GraphClient, GraphError
from .plan import Change, Plan

# What to read back to verify a field before writing it.
_VERIFY_FIELD = {"status": "status", "daily_budget": "daily_budget"}


@dataclass
class Outcome:
    change: Change
    state: str            # "applied" | "skipped" | "failed" | "unchanged"
    detail: str = ""


def _current(client: GraphClient, change: Change) -> Any:
    field = _VERIFY_FIELD[change.field_name]
    node = client.get(change.entity_id, {"fields": field})
    return node.get(field)


def _matches(expected: Any, actual: Any) -> bool:
    """Compare the way Meta returns values: budgets come back as strings."""
    if expected is None or actual is None:
        return expected == actual
    return str(expected).strip() == str(actual).strip()


def rollback_plan(plan: Plan, outcomes: list[Outcome]) -> Plan:
    """The plan that puts back exactly what was actually changed."""
    reverted: list[Change] = []
    for outcome in outcomes:
        if outcome.state != "applied":
            continue
        c = outcome.change
        reverted.append(
            Change(
                kind=c.kind,
                level=c.level,
                entity_id=c.entity_id,
                entity_name=c.entity_name,
                field_name=c.field_name,
                before=c.after,
                after=c.before,
                before_display=c.after_display,
                after_display=c.before_display,
                reason=f"Rollback of: {c.reason}",
                source_check=c.source_check,
                daily_delta=-c.daily_delta,
            )
        )
    return Plan(
        account_id=plan.account_id,
        currency=plan.currency,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        window=plan.window,
        changes=reverted,
        budget_allowance=plan.budget_allowance,
    )


def apply_plan(
    client: GraphClient,
    plan: Plan,
    *,
    rollback_path: str | Path | None = None,
    dry_run: bool = False,
) -> list[Outcome]:
    """Apply every change in ``plan``. Returns one outcome per change."""
    outcomes: list[Outcome] = []

    if rollback_path and not dry_run:
        # Written before the first write, so an interrupted run is recoverable.
        _write_rollback(rollback_path, plan)

    for change in plan.changes:
        try:
            actual = _current(client, change)
        except GraphError as exc:
            outcomes.append(
                Outcome(change, "failed", f"could not read current value: {exc.message}")
            )
            continue

        if _matches(change.after, actual):
            outcomes.append(
                Outcome(change, "unchanged", "already at the target value")
            )
            continue

        if not _matches(change.before, actual):
            outcomes.append(
                Outcome(
                    change,
                    "skipped",
                    f"expected {change.before!r} but the account now has "
                    f"{actual!r} — the plan is stale for this object, "
                    f"re-run --plan",
                )
            )
            continue

        if dry_run:
            outcomes.append(Outcome(change, "skipped", "dry run"))
            continue

        try:
            client.post(change.entity_id, change.api_params())
        except (GraphError, ValueError) as exc:
            outcomes.append(Outcome(change, "failed", str(exc)))
            continue
        outcomes.append(Outcome(change, "applied"))

    return outcomes


def _write_rollback(path: str | Path, plan: Plan) -> Path:
    """Record every current value up front, before anything is written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "note": (
            "Values as the plan expected to find them. Apply this file with "
            "--apply to undo, after checking it still matches the account."
        ),
        "plan": asdict(
            Plan(
                account_id=plan.account_id,
                currency=plan.currency,
                generated_at=dt.datetime.now(dt.timezone.utc).isoformat(
                    timespec="seconds"
                ),
                window=plan.window,
                changes=[
                    Change(
                        kind=c.kind,
                        level=c.level,
                        entity_id=c.entity_id,
                        entity_name=c.entity_name,
                        field_name=c.field_name,
                        before=c.after,
                        after=c.before,
                        before_display=c.after_display,
                        after_display=c.before_display,
                        reason=f"Rollback of: {c.reason}",
                        source_check=c.source_check,
                        daily_delta=-c.daily_delta,
                    )
                    for c in plan.changes
                ],
                budget_allowance=plan.budget_allowance,
            )
        ),
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def summarize(outcomes: list[Outcome], currency: str) -> str:
    from .currency import fmt

    lines: list[str] = []
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.state] = counts.get(outcome.state, 0) + 1

    applied = [o for o in outcomes if o.state == "applied"]
    delta = sum(o.change.daily_delta for o in applied)

    lines.append(
        "  ".join(
            f"{state.upper()}:{count}" for state, count in sorted(counts.items())
        )
        or "nothing to do"
    )
    if applied:
        lines.append(f"Daily spend change: {fmt(delta, currency)}/day")
    lines.append("")
    for outcome in outcomes:
        c = outcome.change
        head = f"[{outcome.state}] {c.entity_name} — {c.field_name}: {c.before_display} -> {c.after_display}"
        lines.append(head)
        if outcome.detail:
            lines.append(f"    {outcome.detail}")
    return "\n".join(lines)
