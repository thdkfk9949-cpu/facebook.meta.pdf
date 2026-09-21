"""Link tagging — whether traffic can be attributed outside Meta at all.

Meta's own reported conversions are the advertiser's most conflicted number:
the platform grades its own homework. Without UTM tags there is no
independent record in analytics to check it against.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from ..config import Thresholds
from ..fetch import Snapshot
from .base import CheckResult, Confidence, Finding, Severity, register

REQUIRED_PARAMS = ("utm_source", "utm_medium", "utm_campaign")


def _tagged_params(url: str, url_tags: str) -> set[str]:
    found: set[str] = set()
    if url:
        found |= set(parse_qs(urlparse(url).query).keys())
    if url_tags:
        # url_tags is stored as a raw query fragment, e.g. "utm_source=fb&..."
        found |= set(parse_qs(url_tags.lstrip("?&")).keys())
    return found


@register("tracking.utm", "Ads with no independent link tagging")
def check_utm(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult("tracking.utm", "Ads with no independent link tagging")
    untagged: list[tuple[str, str, float, list[str]]] = []
    checked = 0
    for ad in snap.ads:
        if not ad.is_delivering:
            continue
        url = ad.destination_url()
        if not url:
            continue  # lead form or app ad; no outbound link to tag
        checked += 1
        present = _tagged_params(url, ad.url_tags())
        missing = [p for p in REQUIRED_PARAMS if p not in present]
        if missing:
            untagged.append((ad.id, ad.name, ad.insights.spend, missing))

    if not checked:
        result.skipped_reason = "no delivering ads have an outbound destination URL"
        return result
    if not untagged:
        return result

    spend = sum(row[2] for row in untagged)
    share = len(untagged) / checked * 100
    result.findings.append(
        Finding(
            check_id="tracking.utm",
            severity=Severity.MEDIUM if share < 50 else Severity.HIGH,
            confidence=Confidence.STRUCTURAL,
            level="account",
            entity_id=snap.account_id,
            entity_name=snap.account_name,
            title=f"{len(untagged)} of {checked} delivering ads are missing UTM tags",
            detail=(
                f"{share:.0f}% of ads with an outbound link lack at least one of "
                f"{', '.join(REQUIRED_PARAMS)}. Traffic from those ads cannot be "
                "separated in analytics, so Meta's conversion figures cannot be "
                "checked against an independent source."
            ),
            recommendation=(
                "Set url_tags at the ad level (or use dynamic parameters like "
                "utm_content={{ad.id}}) so every click is attributable outside "
                "Meta."
            ),
            evidence={
                "untagged_ads": len(untagged),
                "checked_ads": checked,
                "examples": [
                    {"id": i, "name": n, "missing": m} for i, n, _s, m in untagged[:10]
                ],
            },
            spend_at_stake=spend,
        )
    )
    return result
