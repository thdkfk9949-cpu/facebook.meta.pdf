"""Save and reload a snapshot.

Iterating on checks should not cost API quota, and a saved snapshot makes a
finding reproducible: you can hand someone the exact data a report was built
from. Snapshots contain account performance data, so the default .gitignore
excludes them.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .fetch import Ad, AdSet, Campaign, Insights, Snapshot

SCHEMA_VERSION = 1


def dumps(snap: Snapshot) -> str:
    payload = {"schema_version": SCHEMA_VERSION, "snapshot": asdict(snap)}
    return json.dumps(payload, indent=2, ensure_ascii=False)


def save(snap: Snapshot, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(snap), encoding="utf-8")
    return target


def _insights(raw: dict[str, Any] | None) -> Insights:
    return Insights(**raw) if raw else Insights()


def loads(text: str) -> Snapshot:
    payload = json.loads(text)
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"snapshot schema v{version} is not readable by this version "
            f"(expected v{SCHEMA_VERSION}); re-fetch from the API"
        )
    raw = payload["snapshot"]
    campaigns: list[Campaign] = []
    for craw in raw.get("campaigns", []):
        adsets: list[AdSet] = []
        for araw in craw.get("adsets", []):
            ads = [
                Ad(**{**adraw, "insights": _insights(adraw.get("insights"))})
                for adraw in araw.get("ads", [])
            ]
            adsets.append(
                AdSet(
                    **{
                        **araw,
                        "insights": _insights(araw.get("insights")),
                        "prev_insights": _insights(araw.get("prev_insights")),
                        "ads": ads,
                    }
                )
            )
        campaigns.append(
            Campaign(
                **{
                    **craw,
                    "insights": _insights(craw.get("insights")),
                    "adsets": adsets,
                }
            )
        )
    return Snapshot(
        **{
            **raw,
            "campaigns": campaigns,
            "account_insights": _insights(raw.get("account_insights")),
        }
    )


def load(path: str | Path) -> Snapshot:
    return loads(Path(path).read_text(encoding="utf-8"))
