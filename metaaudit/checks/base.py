"""The shape of a finding, and the registry every check registers into."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Thresholds
    from ..fetch import Snapshot


class Severity(enum.IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        return self.name


class Confidence(enum.Enum):
    """How much the finding is worth acting on unreviewed.

    MEASURED  - computed from the account's own numbers, sample sufficient.
    STRUCTURAL- follows from configuration arithmetic, no sample needed.
    HEURISTIC - a pattern that is usually a problem; a human should confirm.
    """

    MEASURED = "measured"
    STRUCTURAL = "structural"
    HEURISTIC = "heuristic"


@dataclass
class Finding:
    check_id: str
    severity: Severity
    confidence: Confidence
    title: str
    detail: str
    recommendation: str
    level: str = "account"          # account | campaign | adset | ad
    entity_id: str = ""
    entity_name: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    # Spend the finding is about, over the audit window. Used for ranking, so
    # a 3-dollar problem never outranks a 3000-dollar one.
    spend_at_stake: float = 0.0

    @property
    def sort_key(self) -> tuple:
        return (-int(self.severity), -self.spend_at_stake, self.check_id)


@dataclass
class CheckResult:
    check_id: str
    title: str
    findings: list[Finding] = field(default_factory=list)
    # Set when the check could not run — missing field, no data, sample too
    # small. Reported as its own section rather than silently passing.
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.findings and not self.skipped_reason


CheckFn = Callable[["Snapshot", "Thresholds"], CheckResult]

_REGISTRY: list[tuple[str, str, CheckFn]] = []


def register(check_id: str, title: str) -> Callable[[CheckFn], CheckFn]:
    def wrap(fn: CheckFn) -> CheckFn:
        _REGISTRY.append((check_id, title, fn))
        return fn

    return wrap


def all_checks() -> list[tuple[str, str, CheckFn]]:
    return list(_REGISTRY)


def run_all(
    snapshot: "Snapshot",
    thresholds: "Thresholds",
    *,
    only: Iterable[str] | None = None,
) -> list[CheckResult]:
    wanted = set(only) if only else None
    results: list[CheckResult] = []
    for check_id, title, fn in _REGISTRY:
        if wanted is not None and check_id not in wanted:
            continue
        try:
            results.append(fn(snapshot, thresholds))
        except Exception as exc:  # a broken check must not kill the report
            results.append(
                CheckResult(
                    check_id=check_id,
                    title=title,
                    skipped_reason=f"check raised {type(exc).__name__}: {exc}",
                )
            )
    return results
