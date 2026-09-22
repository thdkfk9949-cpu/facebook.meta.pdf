"""Rendering. Findings are ranked by severity, then by the money behind them."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Iterable

from .checks.base import CheckResult, Confidence, Finding, Severity
from .config import Thresholds
from .currency import fmt
from .fetch import Snapshot

SEVERITY_MARK = {
    Severity.CRITICAL: "[CRITICAL]",
    Severity.HIGH: "[HIGH]    ",
    Severity.MEDIUM: "[MEDIUM]  ",
    Severity.LOW: "[LOW]     ",
    Severity.INFO: "[INFO]    ",
}


def collect(results: Iterable[CheckResult]) -> list[Finding]:
    findings = [f for r in results for f in r.findings]
    findings.sort(key=lambda f: f.sort_key)
    return findings


def _wrap(text: str, width: int, indent: str) -> str:
    import textwrap

    return "\n".join(
        textwrap.fill(
            para, width=width, initial_indent=indent, subsequent_indent=indent
        )
        for para in text.split("\n")
    )


def render_text(
    snap: Snapshot, results: list[CheckResult], th: Thresholds, width: int = 88
) -> str:
    findings = collect(results)
    out: list[str] = []
    bar = "=" * width

    out.append(bar)
    out.append(f"META ADS ACCOUNT AUDIT  —  {snap.account_name} ({snap.account_id})")
    out.append(bar)
    out.append(f"Window        : {snap.since} to {snap.until} ({snap.window_days} days)")
    out.append(f"Baseline      : {snap.prev_since} to {snap.prev_until}")
    out.append(f"Currency      : {snap.currency}   Timezone: {snap.timezone or 'unknown'}")
    out.append(
        f"Scope         : {len(snap.campaigns)} campaigns, {len(snap.adsets)} ad sets, "
        f"{len(snap.ads)} ads"
    )
    out.append(f"Spend         : {fmt(snap.account_insights.spend, snap.currency)}")
    out.append(f"API requests  : {snap.api_requests}")
    out.append("")

    counts = {s: sum(1 for f in findings if f.severity == s) for s in Severity}
    summary = "  ".join(
        f"{s.label}:{counts[s]}" for s in sorted(Severity, reverse=True) if counts[s]
    )
    out.append(f"FINDINGS      : {len(findings)}   {summary or 'none'}")
    out.append("")

    if snap.warnings:
        out.append("-" * width)
        out.append("DATA WARNINGS")
        for warning in snap.warnings:
            out.append(_wrap(warning, width, "  - "[:2] + "  "))
        out.append("")

    if not findings:
        out.append("No findings. Either the account is clean, or the checks that")
        out.append("would have caught something were skipped — see SKIPPED below.")
        out.append("")

    for finding in findings:
        mark = SEVERITY_MARK[finding.severity]
        scope = f"{finding.level}:{finding.entity_name or finding.entity_id}"
        out.append("-" * width)
        out.append(f"{mark} {finding.title}")
        out.append(f"            {scope}")
        out.append(
            f"            check={finding.check_id}  "
            f"confidence={finding.confidence.value}  "
            f"spend={fmt(finding.spend_at_stake, snap.currency)}"
        )
        out.append("")
        out.append(_wrap(finding.detail, width, "  "))
        out.append("")
        out.append(_wrap(f"FIX: {finding.recommendation}", width, "  "))
        out.append("")

    skipped = [r for r in results if r.skipped_reason]
    if skipped:
        out.append(bar)
        out.append("SKIPPED CHECKS  (a skip is not a pass)")
        out.append(bar)
        for result in skipped:
            out.append(f"  {result.check_id}")
            out.append(_wrap(result.skipped_reason, width, "      "))
            out.append("")

    out.append(bar)
    out.append("HOW TO READ THIS")
    out.append(bar)
    out.append(
        _wrap(
            "confidence=structural means the finding follows from the account's "
            "configuration arithmetic and needs no sample size to be true. "
            "confidence=measured means it cleared a significance test on this "
            "account's own numbers. confidence=heuristic means it is a pattern "
            "that is usually a problem — confirm it before acting.",
            width,
            "  ",
        )
    )
    out.append("")
    out.append(
        _wrap(
            "Fix tracking findings before efficiency ones. An ad set starved "
            "below 50 events/week looks like a poor performer whether or not it "
            "is one, so work the learning and structure findings before pausing "
            "anything.",
            width,
            "  ",
        )
    )
    out.append("")
    return "\n".join(out)


def render_markdown(snap: Snapshot, results: list[CheckResult], th: Thresholds) -> str:
    findings = collect(results)
    out: list[str] = []
    out.append(f"# Meta Ads audit — {snap.account_name}")
    out.append("")
    out.append(f"`{snap.account_id}` · {snap.since} → {snap.until} ({snap.window_days}d) · "
               f"{fmt(snap.account_insights.spend, snap.currency)} spend")
    out.append("")
    # Caveats about the data itself go above the findings: they decide how
    # much any of the numbers below are worth.
    for warning in snap.warnings:
        out.append(f"> **{warning}**")
        out.append("")
    counts = {s: sum(1 for f in findings if f.severity == s) for s in Severity}
    out.append("| Severity | Count |")
    out.append("|---|---|")
    for sev in sorted(Severity, reverse=True):
        if counts[sev]:
            out.append(f"| {sev.label} | {counts[sev]} |")
    out.append("")

    for finding in findings:
        out.append(f"## {finding.severity.label} — {finding.title}")
        out.append("")
        out.append(
            f"**{finding.level}**: {finding.entity_name or finding.entity_id} · "
            f"`{finding.check_id}` · confidence: {finding.confidence.value} · "
            f"spend at stake: {fmt(finding.spend_at_stake, snap.currency)}"
        )
        out.append("")
        out.append(finding.detail)
        out.append("")
        out.append(f"**Fix:** {finding.recommendation}")
        out.append("")

    skipped = [r for r in results if r.skipped_reason]
    if skipped:
        out.append("## Skipped checks")
        out.append("")
        out.append("A skip is not a pass.")
        out.append("")
        for result in skipped:
            out.append(f"- `{result.check_id}` — {result.skipped_reason}")
        out.append("")
    return "\n".join(out)


def render_json(snap: Snapshot, results: list[CheckResult], th: Thresholds) -> str:
    payload = {
        "account": {
            "id": snap.account_id,
            "name": snap.account_name,
            "currency": snap.currency,
            "timezone": snap.timezone,
        },
        "window": {
            "since": snap.since,
            "until": snap.until,
            "days": snap.window_days,
            "baseline_since": snap.prev_since,
            "baseline_until": snap.prev_until,
        },
        "scope": {
            "campaigns": len(snap.campaigns),
            "adsets": len(snap.adsets),
            "ads": len(snap.ads),
            "spend": round(snap.account_insights.spend, 2),
            "api_requests": snap.api_requests,
        },
        "thresholds": th.as_dict(),
        "warnings": snap.warnings,
        "findings": [
            {
                **asdict(f),
                "severity": f.severity.label,
                "confidence": f.confidence.value,
            }
            for f in collect(results)
        ],
        "skipped": [
            {"check_id": r.check_id, "reason": r.skipped_reason}
            for r in results
            if r.skipped_reason
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


RENDERERS = {"text": render_text, "markdown": render_markdown, "json": render_json}
