"""Ad sets inside one campaign that are bidding for the same people.

An honest caveat, stated here and repeated in the report: this is NOT Meta's
Audience Overlap tool. The true overlap percentage between two audiences is
not exposed through the Marketing API, so nothing here is a measured
percentage. What this check does is structural, in two tiers:

1. Exact duplicates — ad sets whose full audience definition is identical.
   They are unambiguously chasing the same people. Structural.
2. Shared demographic base with no mutual exclusion — ad sets on the same
   geo/age/gender footprint whose audiences differ (a broad set next to a
   lookalike, say) and which do not exclude one another. How much they
   actually overlap depends on audience membership we cannot see, so this is
   reported as a heuristic, not a fact.

Tier 2 is where the common waste lives: a prospecting ad set that never
excludes the retargeting audience spends the campaign's money re-buying
people the other ad set already reaches.
"""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations
from typing import Any

from ..config import Thresholds
from ..fetch import AdSet, Snapshot
from .base import CheckResult, Confidence, Finding, Severity, register

# Targeting keys that define *who* is reached. Placement and optimisation keys
# are deliberately excluded: two ad sets differing only by placement are still
# chasing the same people.
AUDIENCE_KEYS = (
    "geo_locations", "excluded_geo_locations", "age_min", "age_max", "genders",
    "custom_audiences", "excluded_custom_audiences", "flexible_spec",
    "interests", "behaviors", "locales", "exclusions",
)
# The subset that defines the demographic footprint two ad sets share even
# when their audience lists differ.
DEMOGRAPHIC_KEYS = (
    "geo_locations", "excluded_geo_locations", "age_min", "age_max",
    "genders", "locales",
)


def _canonical(value: Any) -> Any:
    """Order-insensitive form, so [A,B] and [B,A] fingerprint the same."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return sorted(
            (_canonical(v) for v in value), key=lambda x: json.dumps(x, sort_keys=True)
        )
    return value


def _fingerprint(adset: AdSet, keys: tuple[str, ...]) -> str:
    targeting = adset.targeting or {}
    subset = {k: _canonical(targeting[k]) for k in keys if k in targeting}
    return json.dumps(subset, sort_keys=True, ensure_ascii=False)


def audience_fingerprint(adset: AdSet) -> str:
    return _fingerprint(adset, AUDIENCE_KEYS)


def demographic_fingerprint(adset: AdSet) -> str:
    return _fingerprint(adset, DEMOGRAPHIC_KEYS)


def _audience_ids(adset: AdSet, key: str) -> set[str]:
    raw = (adset.targeting or {}).get(key) or []
    out: set[str] = set()
    for item in raw:
        if isinstance(item, dict) and item.get("id") is not None:
            out.add(str(item["id"]))
        elif isinstance(item, (str, int)):
            out.add(str(item))
    return out


def has_mutual_exclusion(a: AdSet, b: AdSet) -> bool:
    """True when each ad set excludes an audience the other targets.

    Only meaningful between ad sets whose audiences differ — two ad sets with
    identical definitions cannot exclude each other by construction, which is
    why tier 1 never consults this.
    """
    a_in, a_ex = _audience_ids(a, "custom_audiences"), _audience_ids(a, "excluded_custom_audiences")
    b_in, b_ex = _audience_ids(b, "custom_audiences"), _audience_ids(b, "excluded_custom_audiences")
    return bool((a_ex & b_in) and (b_ex & a_in))


@register("structure.self_competition", "Ad sets competing against each other")
def check_self_competition(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult(
        "structure.self_competition", "Ad sets competing against each other"
    )
    any_targeting = False

    for campaign in snap.campaigns:
        if not campaign.is_delivering:
            continue
        members = [a for a in campaign.active_adsets if a.targeting]
        if len(members) < 2:
            continue
        any_targeting = True

        by_demo: dict[str, list[AdSet]] = defaultdict(list)
        for adset in members:
            by_demo[demographic_fingerprint(adset)].append(adset)

        for demo_fp, cohort in by_demo.items():
            if len(cohort) < 2:
                continue

            # --- tier 1: identical audience definitions ---------------------
            by_audience: dict[str, list[AdSet]] = defaultdict(list)
            for adset in cohort:
                by_audience[audience_fingerprint(adset)].append(adset)

            for audience_fp, dupes in by_audience.items():
                if len(dupes) < 2:
                    continue
                result.findings.append(
                    Finding(
                        check_id="structure.self_competition",
                        severity=Severity.HIGH,
                        confidence=Confidence.STRUCTURAL,
                        level="campaign",
                        entity_id=campaign.id,
                        entity_name=campaign.name,
                        title=f"{len(dupes)} ad sets share identical targeting",
                        detail=(
                            "These ad sets have the same audience definition: "
                            f"{', '.join(d.name or d.id for d in dupes)}. They "
                            "enter the same auctions for the same people, so the "
                            "account bids against itself and splits the events "
                            "each of them needs to exit the learning phase."
                        ),
                        recommendation=(
                            "Merge them into one ad set. If they exist to test "
                            "creative, that test belongs inside a single ad set "
                            "as separate ads, not as duplicate ad sets."
                        ),
                        evidence={
                            "tier": "identical",
                            "adsets": [{"id": d.id, "name": d.name} for d in dupes],
                            "shared_targeting": json.loads(audience_fp or "{}"),
                        },
                        spend_at_stake=sum(d.insights.spend for d in dupes),
                    )
                )

            # --- tier 2: same footprint, different audiences, no exclusion ---
            representatives = [group[0] for group in by_audience.values()]
            if len(representatives) < 2:
                continue
            unprotected = [
                (a, b)
                for a, b in combinations(representatives, 2)
                if not has_mutual_exclusion(a, b)
            ]
            if not unprotected:
                continue
            involved = {a.id: a for pair in unprotected for a in pair}
            result.findings.append(
                Finding(
                    check_id="structure.self_competition",
                    severity=Severity.MEDIUM,
                    # The demographic footprint is a fact; how much the
                    # audiences actually overlap inside it is not observable.
                    confidence=Confidence.HEURISTIC,
                    level="campaign",
                    entity_id=campaign.id,
                    entity_name=campaign.name,
                    title=(
                        f"{len(involved)} ad sets share a demographic footprint "
                        "with no mutual exclusion"
                    ),
                    detail=(
                        "These ad sets target the same geography, age range and "
                        "gender, but neither excludes the other's audience: "
                        f"{', '.join(a.name or a.id for a in involved.values())}. "
                        "Wherever their audiences overlap, the account bids "
                        "against itself. How large that overlap is depends on "
                        "audience membership the API does not expose — check it "
                        "in Ads Manager's Audience Overlap tool."
                    ),
                    recommendation=(
                        "Add the narrower ad set's audience to the broader one's "
                        "exclusions — retargeting and converter audiences "
                        "excluded from prospecting, for instance. If the overlap "
                        "turns out to be small, leave them and consolidate "
                        "budget instead."
                    ),
                    evidence={
                        "tier": "shared_footprint",
                        "adsets": [
                            {"id": a.id, "name": a.name} for a in involved.values()
                        ],
                        "shared_demographics": json.loads(demo_fp or "{}"),
                        "unprotected_pairs": [
                            [a.id, b.id] for a, b in unprotected
                        ],
                    },
                    spend_at_stake=sum(a.insights.spend for a in involved.values()),
                )
            )

    if not any_targeting and not result.findings:
        result.skipped_reason = (
            "no targeting specs were returned — the token may lack permission "
            "to read targeting on this account"
        )
    return result
