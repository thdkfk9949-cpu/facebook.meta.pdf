"""Funnel diagnosis — when an ad set costs more, which part should change.

"Change the audience" and "change the creative" are different fixes, and
picking the wrong one wastes a learning reset. The funnel tells them apart:

    impressions --(link CTR)--> link clicks --(load rate)--> landing page
    views --(conversion rate)--> conversions

An ad set whose CPA is worse than its peers is walked down that funnel and
each stage rate is compared against the pooled peers. The first stage that
is both significantly and materially worse is the diagnosis:

- link CTR low            -> the ad is not earning the click: creative
- landing page views low  -> people click but the page does not load: site
- conversion rate low     -> people arrive but do not buy: page / offer
- rates fine, CPM high    -> the ad works, the audience is expensive: audience
- nothing testable        -> not enough data: change nothing yet

Peers are the rest of the ad set's own campaign when it has any, because
that holds objective and audience strategy roughly constant (a retargeting
ad set converts better than a prospecting one for reasons that are not a
defect). Without campaign peers it falls back to the rest of the account and
downgrades the confidence accordingly.

Ad sets in the learning phase are left to ``learning.stuck``: their numbers
are not settled, and the advice there is already "do not touch".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import Thresholds
from ..currency import fmt
from ..fetch import AdSet, Insights, Snapshot
from ..stats import two_proportion_p
from .base import CheckResult, Confidence, Finding, Severity, register

CHECK_ID = "diagnosis.funnel"
TITLE = "Where an underperforming ad set's funnel breaks"

LINK_CLICK = "link_click"
LANDING_PAGE_VIEW = "landing_page_view"


@dataclass
class Stage:
    key: str
    label: str
    mine: float
    peers: float
    gap_pct: float          # how far below peers, relative; negative = better
    p_value: float | None

    def broken(self, th: Thresholds) -> bool:
        return (
            self.p_value is not None
            and self.p_value <= th.alpha
            and self.gap_pct >= th.diag_min_gap_pct
        )

    def describe(self, alpha: float) -> str:
        if self.p_value is None:
            test = "too few events to test"
        elif self.p_value <= alpha:
            test = f"p={self.p_value:.4f}"
        else:
            test = "not significant"
        direction = "below" if self.gap_pct >= 0 else "above"
        return (
            f"{self.label} {self.mine * 100:.2f}% vs {self.peers * 100:.2f}% "
            f"({abs(self.gap_pct):.0f}% {direction} peers, {test})"
        )


def _stage(
    key: str, label: str,
    mine_num: float, mine_den: float, rest_num: float, rest_den: float,
) -> Stage | None:
    if mine_den <= 0 or rest_den <= 0 or rest_num <= 0:
        return None
    mine = mine_num / mine_den
    peers = rest_num / rest_den
    return Stage(
        key=key,
        label=label,
        mine=mine,
        peers=peers,
        gap_pct=(peers - mine) / peers * 100.0,
        p_value=two_proportion_p(
            int(mine_num), int(mine_den), int(rest_num), int(rest_den)
        ),
    )


def _peers(snap: Snapshot, adset: AdSet, actions: Sequence[str]) -> tuple[list[AdSet], str]:
    def comparable(other: AdSet) -> bool:
        return (
            other.id != adset.id
            and other.is_delivering
            and other.optimizes_for_conversions
            and other.conversion_action_types() == tuple(actions)
            and other.insights.impressions > 0
        )

    campaign = snap.campaign_of(adset)
    if campaign is not None:
        siblings = [a for a in campaign.adsets if comparable(a)]
        if siblings:
            return siblings, "campaign"
    return [a for a in snap.adsets if comparable(a)], "account"


def _pool(adsets: Sequence[AdSet]) -> Insights:
    pooled = Insights()
    for adset in adsets:
        ins = adset.insights
        pooled.spend += ins.spend
        pooled.impressions += ins.impressions
        pooled.clicks += ins.clicks
        for key, value in ins.actions.items():
            pooled.actions[key] = pooled.actions.get(key, 0.0) + value
    return pooled


def _has(group: Sequence[AdSet], action: str) -> bool:
    """A funnel step is only usable when every ad set in the comparison reports it.

    Mixing link clicks for one side with all clicks for the other would
    manufacture a CTR gap out of a definition difference.
    """
    return all(a.insights.actions.get(action, 0) > 0 for a in group)


VERDICTS = {
    "click": (
        "creative",
        "Change the creative — people see the ad and do not click",
        (
            "Ship new creative with a different hook and visual, not a recoloured "
            "copy. Leave the targeting alone for now: if two or three genuinely "
            "different angles all lose on link CTR against the same audience, "
            "then the audience is the problem."
        ),
    ),
    "page_load": (
        "page_load",
        "Fix the landing page load — people click but never arrive",
        (
            "Neither the ad nor the audience is the problem. Check page speed on "
            "mobile, redirects, and that the pixel fires on page load. Changing "
            "creative or targeting here only resets learning."
        ),
    ),
    "conversion": (
        "post_click",
        "Fix the page or offer — people arrive but do not convert",
        (
            "The ad earns the click, so the break is after it: landing page "
            "match with the ad's promise, price, form length, checkout. Confirm "
            "the conversion event fires correctly before redesigning anything."
        ),
    ),
}


@register(CHECK_ID, TITLE)
def check_funnel(snap: Snapshot, th: Thresholds) -> CheckResult:
    result = CheckResult(CHECK_ID, TITLE)
    cur = snap.currency
    account_spend = snap.account_insights.spend or sum(
        c.insights.spend for c in snap.campaigns
    )

    candidates = [
        a for a in snap.adsets
        if a.is_delivering and a.optimizes_for_conversions and a.insights.impressions > 0
    ]
    if not candidates:
        result.skipped_reason = (
            "no delivering ad sets optimize for a conversion event, so there is "
            "no funnel to diagnose"
        )
        return result

    compared = 0
    for adset in candidates:
        if adset.is_learning:
            continue
        if account_spend > 0 and (
            adset.insights.spend / account_spend < th.min_spend_share_to_report
        ):
            continue
        actions = adset.conversion_action_types()
        peers, scope = _peers(snap, adset, actions)
        if not peers:
            continue
        compared += 1

        mine, rest = adset.insights, _pool(peers)
        group = [adset, *peers]
        mine_conv, rest_conv = mine.conversions(actions), rest.conversions(actions)
        if rest_conv <= 0:
            continue  # peers convert nothing: a tracking question, not a funnel one

        rest_cpa = rest.spend / rest_conv
        mine_cpa = mine.spend / mine_conv if mine_conv else float("inf")
        cpa_gap_pct = (mine_cpa - rest_cpa) / rest_cpa * 100.0
        if cpa_gap_pct < th.diag_min_gap_pct:
            continue  # not underperforming: nothing to diagnose

        use_link = _has(group, LINK_CLICK)
        use_lpv = use_link and _has(group, LANDING_PAGE_VIEW)
        mine_clicks = mine.actions[LINK_CLICK] if use_link else mine.clicks
        rest_clicks = rest.actions[LINK_CLICK] if use_link else rest.clicks
        ctr_label = "link CTR" if use_link else "CTR (all clicks)"

        stages: list[Stage] = []
        click = _stage("click", ctr_label, mine_clicks, mine.impressions,
                       rest_clicks, rest.impressions)
        if click:
            stages.append(click)
        if use_lpv:
            mine_lpv = mine.actions[LANDING_PAGE_VIEW]
            rest_lpv = rest.actions[LANDING_PAGE_VIEW]
            load = _stage("page_load", "click-to-page-view rate",
                          mine_lpv, mine_clicks, rest_lpv, rest_clicks)
            if load:
                stages.append(load)
            conv = _stage("conversion", "page-view-to-conversion rate",
                          mine_conv, mine_lpv, rest_conv, rest_lpv)
        else:
            conv = _stage("conversion", "click-to-conversion rate",
                          mine_conv, mine_clicks, rest_conv, rest_clicks)
        if conv:
            stages.append(conv)

        mine_cpm = mine.spend / mine.impressions * 1000.0
        rest_cpm = rest.spend / rest.impressions * 1000.0 if rest.impressions else 0.0
        cpm_ratio = mine_cpm / rest_cpm if rest_cpm > 0 else 0.0

        broken = [s for s in stages if s.broken(th)]
        enough = mine_conv >= th.min_conversions_for_claim
        cpa_text = (
            f"CPA {fmt(mine_cpa, cur)} vs {fmt(rest_cpa, cur)} for peers "
            f"({cpa_gap_pct:.0f}% worse)"
            if mine_conv
            else f"no conversions vs a peer CPA of {fmt(rest_cpa, cur)}"
        )
        peer_text = (
            f"the other {len(peers)} ad set(s) in its campaign"
            if scope == "campaign"
            else (
                f"{len(peers)} ad set(s) elsewhere in the account (it has no "
                "comparable siblings in its own campaign, so differences in "
                "audience strategy may explain part of the gap)"
            )
        )
        funnel_text = "; ".join(s.describe(th.alpha) for s in stages)
        cpm_text = f"CPM {fmt(mine_cpm, cur)} vs {fmt(rest_cpm, cur)}"

        if broken:
            primary = broken[0]
            verdict, title, recommendation = VERDICTS[primary.key]
            measured = scope == "campaign"
            extra = ""
            if verdict == "creative" and mine.frequency >= th.frequency_warn:
                extra = (
                    f" Frequency is {mine.frequency:.1f}, so this may be "
                    "wear-out rather than a weak ad from the start — see "
                    "delivery.fatigue."
                )
            if len(broken) > 1:
                extra += (
                    " Later stages are also below peers ("
                    + ", ".join(s.label for s in broken[1:])
                    + "); fix the earliest break first, since it caps everything "
                    "after it."
                )
            severity = Severity.MEDIUM if measured else Severity.LOW
            confidence = Confidence.MEASURED if measured else Confidence.HEURISTIC
            detail = (
                f"{cpa_text}, compared with {peer_text}. Walking the funnel: "
                f"{funnel_text}. {cpm_text}. The first significant break is "
                f"{primary.label}.{extra}"
            )
        elif cpm_ratio >= th.diag_cpm_ratio:
            saturated = mine.frequency >= th.frequency_warn
            verdict = "audience"
            title = (
                "Broaden the audience — it is small and saturated"
                if saturated
                else "Look at the audience — the ad works, the reach is expensive"
            )
            recommendation = (
                "Click and conversion rates are in line with peers, so the ad is "
                "not the problem. Widen the targeting (or switch to Advantage+ "
                "audience) and check structure.self_competition for siblings "
                "bidding on the same people. CPM is also driven by competition "
                "and ad quality ranking, so confirm in Ads Manager before "
                "rebuilding the ad set."
            )
            severity = Severity.LOW
            confidence = Confidence.HEURISTIC
            detail = (
                f"{cpa_text}, compared with {peer_text}. {cpm_text} "
                f"({cpm_ratio:.1f}x peers)"
                + (f" at frequency {mine.frequency:.1f}" if saturated else "")
                + f". Funnel rates show no significant break: {funnel_text}."
                + (
                    ""
                    if enough
                    else (
                        f" Only {int(mine_conv)} conversions, so the CPA itself "
                        "is not decision-grade; the CPM gap is the firmer signal."
                    )
                )
            )
        elif not enough:
            verdict = "insufficient_data"
            title = "Looks worse, but there is not enough data to say why"
            recommendation = (
                "Change nothing yet — neither the audience nor the creative. A "
                "change now resets learning on the strength of noise. Let it reach "
                f"{th.min_conversions_for_claim} conversions, or consolidate it "
                "with a sibling so it gets there sooner."
            )
            severity = Severity.INFO
            confidence = Confidence.MEASURED
            detail = (
                f"{cpa_text}, compared with {peer_text}, but no funnel stage is "
                f"significantly below peers: {funnel_text}. {cpm_text}. With "
                f"{int(mine_conv)} conversions the gap is within what chance "
                "produces."
            )
        else:
            verdict = "no_single_cause"
            title = "Costs more, but no single funnel stage is to blame"
            recommendation = (
                "Each stage is a little worse and none is clearly broken, so "
                "there is no targeted fix to make. Consider consolidating it into "
                "the better-performing sibling rather than rebuilding it."
            )
            severity = Severity.INFO
            confidence = Confidence.HEURISTIC
            detail = (
                f"{cpa_text}, compared with {peer_text}. {funnel_text}. "
                f"{cpm_text}."
            )

        result.findings.append(
            Finding(
                check_id=CHECK_ID,
                severity=severity,
                confidence=confidence,
                level="adset",
                entity_id=adset.id,
                entity_name=adset.name,
                title=title,
                detail=detail,
                recommendation=recommendation,
                evidence={
                    "verdict": verdict,
                    "peer_scope": scope,
                    "peer_adsets": len(peers),
                    "cpa": round(mine_cpa, 2) if mine_conv else None,
                    "peer_cpa": round(rest_cpa, 2),
                    "cpa_gap_pct": round(cpa_gap_pct, 1) if mine_conv else None,
                    "conversions": int(mine_conv),
                    "cpm_ratio": round(cpm_ratio, 2),
                    "uses_link_clicks": use_link,
                    "stages": {
                        s.key: {
                            "rate_pct": round(s.mine * 100, 3),
                            "peer_rate_pct": round(s.peers * 100, 3),
                            "gap_pct": round(s.gap_pct, 1),
                            "p_value": (
                                round(s.p_value, 6) if s.p_value is not None else None
                            ),
                            "broken": s.broken(th),
                        }
                        for s in stages
                    },
                },
                spend_at_stake=mine.spend,
            )
        )

    if compared == 0 and not result.findings:
        result.skipped_reason = (
            "no settled ad set has a comparable peer (another delivering ad set "
            "optimizing for the same event), so there is nothing to diagnose "
            "against"
        )
    return result
