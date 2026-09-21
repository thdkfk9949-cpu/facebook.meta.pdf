"""A synthetic account with one known defect per check.

Every number here is chosen so the expected finding is arithmetically
unambiguous, which is what lets the tests assert on exact values instead of
just "something fired".
"""

from __future__ import annotations

from metaaudit.fetch import Ad, AdSet, Campaign, Insights, Snapshot


def ins(
    spend=0.0, impressions=0, clicks=0, reach=0, frequency=0.0,
    purchases=0.0, revenue=0.0,
) -> Insights:
    return Insights(
        spend=spend,
        impressions=impressions,
        clicks=clicks,
        reach=reach,
        frequency=frequency,
        ctr=(clicks / impressions * 100) if impressions else 0.0,
        actions={"offsite_conversion.fb_pixel_purchase": purchases} if purchases else {},
        action_values=(
            {"offsite_conversion.fb_pixel_purchase": revenue} if revenue else {}
        ),
    )


PIXEL = {"pixel_id": "999", "custom_event_type": "PURCHASE"}
TARGETING_A = {
    "geo_locations": {"countries": ["KR"]},
    "age_min": 25,
    "age_max": 54,
    "genders": [1, 2],
}
# Same audience, keys and list order shuffled — must fingerprint identically.
TARGETING_A_SHUFFLED = {
    "genders": [2, 1],
    "age_max": 54,
    "age_min": 25,
    "geo_locations": {"countries": ["KR"]},
}
TARGETING_B = {
    "geo_locations": {"countries": ["JP"]},
    "age_min": 18,
    "age_max": 34,
}


def _ad(ad_id, adset_id, campaign_id, text, link="", tags="", insights=None) -> Ad:
    return Ad(
        id=ad_id,
        name=f"ad-{ad_id}",
        adset_id=adset_id,
        campaign_id=campaign_id,
        status="ACTIVE",
        effective_status="ACTIVE",
        creative={
            "id": f"cr-{ad_id}",
            "url_tags": tags,
            "object_story_spec": {"link_data": {"message": text, "link": link}},
        },
        insights=insights or ins(),
    )


def build_account() -> Snapshot:
    # --- Campaign 1: sales campaign, badly fragmented -----------------------
    # CPA is 50 (5000 spend / 100 purchases). Exiting learning needs
    # 50 * 50 / 7 = 357.14/day per ad set. Campaign has 400/day across 4 ad
    # sets = 100/day each, which supports int(400 // 357.14) = 1 ad set.
    c1_adsets = []
    for i in range(1, 5):
        c1_adsets.append(
            AdSet(
                id=f"as1{i}",
                name=f"c1-adset-{i}",
                campaign_id="c1",
                status="ACTIVE",
                effective_status="ACTIVE",
                optimization_goal="OFFSITE_CONVERSIONS",
                billing_event="IMPRESSIONS",
                promoted_object=dict(PIXEL),
                attribution_spec=[{"event_type": "CLICK_THROUGH", "window_days": 7}],
                targeting=dict(TARGETING_B) if i > 2 else dict(TARGETING_A),
                insights=ins(spend=1250, impressions=200_000, clicks=4000, purchases=25),
                prev_insights=ins(spend=1250, impressions=200_000, clicks=4000, purchases=25),
                learning_stage_info={"status": "SUCCESS"},
            )
        )
    # Ad sets 3 and 4 share TARGETING_B with no mutual exclusion -> self-competition.
    c1_adsets[3].targeting = dict(TARGETING_B)

    for adset in c1_adsets:
        adset.ads = [
            _ad(f"{adset.id}-a{n}", adset.id, "c1", f"hook number {n}",
                link="https://shop.example.com/p/1?utm_source=fb&utm_medium=paid&utm_campaign=x",
                insights=ins(spend=400, impressions=60_000, clicks=1300, purchases=8))
            for n in range(1, 4)
        ]

    c1 = Campaign(
        id="c1",
        name="Sales — fragmented",
        status="ACTIVE",
        effective_status="ACTIVE",
        objective="OUTCOME_SALES",
        daily_budget=400.0,
        insights=ins(spend=5000, impressions=800_000, clicks=16_000, purchases=100),
        adsets=c1_adsets,
    )

    # --- Campaign 2: sales objective, one ad set buying clicks --------------
    c2_bad = AdSet(
        id="as21",
        name="c2-buying-clicks",
        campaign_id="c2",
        status="ACTIVE",
        effective_status="ACTIVE",
        optimization_goal="LINK_CLICKS",
        billing_event="LINK_CLICKS",
        promoted_object={},
        daily_budget=100.0,
        targeting=dict(TARGETING_A),
        insights=ins(spend=3000, impressions=500_000, clicks=20_000, purchases=4),
        prev_insights=ins(spend=3000, impressions=500_000, clicks=20_000, purchases=4),
        learning_stage_info={"status": "SUCCESS"},
    )
    # Untagged destination -> utm finding. Only one ad -> creative.supply.
    c2_bad.ads = [
        _ad("as21-a1", "as21", "c2", "cheap clicks",
            link="https://shop.example.com/landing",
            insights=ins(spend=3000, impressions=500_000, clicks=20_000, purchases=4))
    ]

    # Fatigued ad set: frequency 5.2, CTR fell 2.00% -> 1.00% on large samples.
    c2_tired = AdSet(
        id="as22",
        name="c2-fatigued",
        campaign_id="c2",
        status="ACTIVE",
        effective_status="ACTIVE",
        optimization_goal="OFFSITE_CONVERSIONS",
        billing_event="IMPRESSIONS",
        promoted_object=dict(PIXEL),
        daily_budget=500.0,
        targeting=dict(TARGETING_A_SHUFFLED),
        insights=ins(spend=4000, impressions=400_000, clicks=4_000, frequency=5.2, purchases=60),
        prev_insights=ins(spend=4000, impressions=400_000, clicks=8_000, frequency=2.1, purchases=90),
        learning_stage_info={"status": "SUCCESS"},
    )
    c2_tired.ads = [
        _ad(f"as22-a{n}", "as22", "c2", f"tired hook {n}",
            link="https://shop.example.com/p/2?utm_source=fb&utm_medium=paid&utm_campaign=y",
            insights=ins(spend=1300, impressions=133_000, clicks=1_333, purchases=20))
        for n in range(1, 4)
    ]

    # Learning-limited ad set, no attribution spec -> attribution_mix in c2.
    c2_stuck = AdSet(
        id="as23",
        name="c2-learning-limited",
        campaign_id="c2",
        status="ACTIVE",
        effective_status="ACTIVE",
        optimization_goal="OFFSITE_CONVERSIONS",
        billing_event="IMPRESSIONS",
        promoted_object=dict(PIXEL),
        daily_budget=30.0,
        attribution_spec=[{"event_type": "CLICK_THROUGH", "window_days": 1}],
        targeting=dict(TARGETING_B),
        insights=ins(spend=900, impressions=40_000, clicks=600, purchases=6),
        prev_insights=ins(spend=900, impressions=40_000, clicks=600, purchases=6),
        learning_stage_info={"status": "FAIL", "conversions": 6},
    )
    c2_stuck.ads = [
        _ad("as23-a1", "as23", "c2", "a", link="https://x.example.com/?utm_source=fb&utm_medium=paid&utm_campaign=z",
            insights=ins(spend=900, impressions=40_000, clicks=600, purchases=6)),
        _ad("as23-a2", "as23", "c2", "b", link="https://x.example.com/?utm_source=fb&utm_medium=paid&utm_campaign=z"),
        _ad("as23-a3", "as23", "c2", "c", link="https://x.example.com/?utm_source=fb&utm_medium=paid&utm_campaign=z"),
    ]

    c2 = Campaign(
        id="c2",
        name="Sales — misconfigured",
        status="ACTIVE",
        effective_status="ACTIVE",
        objective="OUTCOME_SALES",
        insights=ins(spend=7900, impressions=940_000, clicks=24_600, purchases=70),
        adsets=[c2_bad, c2_tired, c2_stuck],
    )

    snap = Snapshot(
        account_id="act_1",
        account_name="Test Account",
        currency="USD",
        timezone="Asia/Seoul",
        window_days=30,
        since="2026-08-22",
        until="2026-09-20",
        prev_since="2026-07-23",
        prev_until="2026-08-21",
        campaigns=[c1, c2],
        account_insights=ins(spend=12_900, impressions=1_740_000, clicks=40_600, purchases=170),
        api_requests=7,
    )
    return snap
