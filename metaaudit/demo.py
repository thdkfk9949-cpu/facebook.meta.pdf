"""A synthetic ad account, so the tool can be seen working before a token exists.

None of this is real. The account is a plausible small Korean commerce
account carrying the defects this tool exists to find, and every number is
chosen so the arithmetic in the report can be checked by hand:

    Campaign 1 spends 1,000,000 KRW/day and records a 50,000 KRW CPA.
    Leaving the learning phase takes 50 events per 7 days, so one ad set
    needs 50,000 x 50 / 7 = 357,143 KRW/day. The budget is split four ways
    -- 250,000 each -- so all four ad sets are starved, and the campaign
    budget supports two such ad sets, not four.

That is the worked example the README opens with, rendered as an account.

The demo is deliberately not the test fixture. The fixture in ``tests/`` is
stripped to one unambiguous defect per check; this account has to read like
something an operator would recognise, with a campaign that is configured
correctly and produces no findings at all -- a tool that flags everything is
as useless as one that flags nothing.

``tests/test_demo.py`` pins the findings this account produces, so it cannot
quietly drift into claiming something the checks no longer say.
"""

from __future__ import annotations

from .fetch import Ad, AdSet, Campaign, Insights, Snapshot, _windows

# The demo data describes one fixed window; --window cannot reshape it.
WINDOW_DAYS = 30
# The account currency. KRW has no minor unit, which is the case most easily
# got wrong elsewhere in the tool, so the demo exercises it.
CURRENCY = "KRW"
ACCOUNT_ID = "act_DEMO"
ACCOUNT_NAME = "데모 커머스 (DEMO — synthetic data)"

BANNER = (
    "DEMO DATA — this account does not exist. Every figure below was "
    "invented to show what the audit reports on; none of it came from a "
    "real ad account. Run without --demo to audit yours."
)

PURCHASE = "offsite_conversion.fb_pixel_purchase"
PIXEL = {"pixel_id": "demo-pixel-1", "custom_event_type": "PURCHASE"}
CLICK_7D = [{"event_type": "CLICK_THROUGH", "window_days": 7}]
CLICK_1D = [{"event_type": "CLICK_THROUGH", "window_days": 1}]

# Average order value, used only to give the demo believable revenue figures.
AOV = 90_000.0

# --- audiences -------------------------------------------------------------
# Ad sets 1 and 2 of campaign 1 share this exactly: someone duplicated an ad
# set to test a creative, which is the commonest way an account ends up
# bidding against itself.
BROAD_KR = {
    "geo_locations": {"countries": ["KR"]},
    "age_min": 25,
    "age_max": 54,
    "genders": [1, 2],
}
LOOKALIKE_KR = {
    **BROAD_KR,
    "custom_audiences": [{"id": "demo-la-1", "name": "구매자 유사타겟 1%"}],
}
INTEREST_KR = {
    **BROAD_KR,
    "interests": [{"id": "demo-int-1", "name": "온라인 쇼핑"}],
}
CART_ABANDONERS = {
    "geo_locations": {"countries": ["KR"]},
    "age_min": 20,
    "age_max": 59,
    "custom_audiences": [{"id": "demo-ca-cart", "name": "장바구니 이탈 7일"}],
}


def _ins(
    *,
    spend: float = 0.0,
    impressions: int = 0,
    clicks: int = 0,
    frequency: float = 0.0,
    purchases: float = 0.0,
) -> Insights:
    """Build one window of metrics, with the derived fields kept consistent.

    reach, ctr and cpm are computed rather than typed in so the demo cannot
    contain a row that contradicts itself.
    """
    return Insights(
        spend=spend,
        impressions=impressions,
        clicks=clicks,
        reach=int(impressions / frequency) if frequency else impressions,
        frequency=frequency,
        ctr=(clicks / impressions * 100) if impressions else 0.0,
        cpm=(spend / impressions * 1000) if impressions else 0.0,
        actions={PURCHASE: purchases} if purchases else {},
        action_values={PURCHASE: purchases * AOV} if purchases else {},
    )


def _ad(
    ad_id: str,
    adset: AdSet,
    name: str,
    text: str,
    *,
    path: str,
    tagged: bool = True,
    insights: Insights | None = None,
) -> Ad:
    query = (
        "?utm_source=facebook&utm_medium=paid_social"
        f"&utm_campaign={adset.campaign_id}&utm_content={ad_id}"
        if tagged
        else ""
    )
    return Ad(
        id=ad_id,
        name=name,
        adset_id=adset.id,
        campaign_id=adset.campaign_id,
        status="ACTIVE",
        effective_status="ACTIVE",
        creative={
            "id": f"creative-{ad_id}",
            "url_tags": "",
            "object_story_spec": {
                "link_data": {
                    "message": text,
                    "link": f"https://demo-shop.example.com{path}{query}",
                }
            },
        },
        insights=insights or _ins(),
    )


def _split(total: float, parts: int) -> list[float]:
    """Split a per-ad-set figure across its ads without losing the remainder."""
    each = round(total / parts, 2)
    return [each] * (parts - 1) + [round(total - each * (parts - 1), 2)]


def _spread(adset: AdSet, ads: list[tuple[str, str, str]], *, tagged: bool) -> None:
    """Attach ads to an ad set, dividing its spend and conversions between them."""
    spends = _split(adset.insights.spend, len(ads))
    impressions = _split(adset.insights.impressions, len(ads))
    clicks = _split(adset.insights.clicks, len(ads))
    purchases = _split(adset.insights.conversions([PURCHASE]), len(ads))
    adset.ads = [
        _ad(
            f"{adset.id}-ad{n}",
            adset,
            name,
            text,
            path=path,
            tagged=tagged,
            insights=_ins(
                spend=spends[n - 1],
                impressions=int(impressions[n - 1]),
                clicks=int(clicks[n - 1]),
                frequency=adset.insights.frequency,
                purchases=purchases[n - 1],
            ),
        )
        for n, (name, text, path) in enumerate(ads, 1)
    ]


def _prospecting_campaign() -> Campaign:
    """Campaign budget optimisation, split four ways, starving all four.

    Spend is 30,000,000 over 600 purchases: a CPA of exactly 50,000. The
    1,000,000/day budget divided by four gives each ad set 250,000/day, which
    buys 35 events a week against the 50 needed.
    """
    campaign = Campaign(
        id="demo-c1",
        name="신규 고객 유입 — 전환",
        status="ACTIVE",
        effective_status="ACTIVE",
        objective="OUTCOME_SALES",
        buying_type="AUCTION",
        daily_budget=1_000_000.0,
        bid_strategy="LOWEST_COST_WITHOUT_CAP",
        insights=_ins(
            spend=30_000_000, impressions=1_530_000, clicks=30_600,
            frequency=2.9, purchases=600,
        ),
    )

    def adset(suffix: str, name: str, targeting: dict, **metrics) -> AdSet:
        return AdSet(
            id=f"demo-c1-as{suffix}",
            name=name,
            campaign_id=campaign.id,
            status="ACTIVE",
            effective_status="ACTIVE",
            optimization_goal="OFFSITE_CONVERSIONS",
            billing_event="IMPRESSIONS",
            promoted_object=dict(PIXEL),
            attribution_spec=[dict(spec) for spec in CLICK_7D],
            targeting=targeting,
            learning_stage_info={"status": "SUCCESS"},
            **metrics,
        )

    # Ad sets 1 and 2 are the accidental duplicate pair: byte-identical
    # targeting, so they enter the same auctions for the same people.
    first = adset(
        "1", "신규-광범위 A", dict(BROAD_KR),
        insights=_ins(spend=8_000_000, impressions=400_000, clicks=8_000,
                      frequency=2.6, purchases=180),
        prev_insights=_ins(spend=7_800_000, impressions=380_000, clicks=7_800,
                           frequency=2.4, purchases=172),
    )
    second = adset(
        "2", "신규-광범위 B (복제본)", dict(BROAD_KR),
        insights=_ins(spend=8_000_000, impressions=400_000, clicks=8_000,
                      frequency=2.4, purchases=170),
        prev_insights=_ins(spend=7_900_000, impressions=390_000, clicks=7_900,
                           frequency=2.3, purchases=168),
    )
    # Fatigued: frequency has climbed to 3.6 while CTR fell 3.00% -> 2.00%,
    # on impression counts large enough for the drop to clear the test.
    third = adset(
        "3", "신규-유사타겟 1%", dict(LOOKALIKE_KR),
        insights=_ins(spend=7_500_000, impressions=380_000, clicks=7_600,
                      frequency=3.6, purchases=160),
        prev_insights=_ins(spend=7_500_000, impressions=380_000, clicks=11_400,
                           frequency=2.2, purchases=205),
    )
    # Converts at 1.29% against 2.16% for its siblings — a real difference on
    # 90 conversions, which is enough sample to say so out loud.
    fourth = adset(
        "4", "신규-관심사 (온라인 쇼핑)", dict(INTEREST_KR),
        insights=_ins(spend=6_500_000, impressions=350_000, clicks=7_000,
                      frequency=2.9, purchases=90),
        prev_insights=_ins(spend=6_400_000, impressions=340_000, clicks=6_900,
                           frequency=2.7, purchases=88),
    )
    campaign.adsets = [first, second, third, fourth]

    texts = [
        ("첫구매 15%", "첫 주문 15% 할인 — 오늘만", "/products/starter"),
        ("무료배송", "3만원 이상 무료배송, 내일 도착", "/products/starter"),
        ("후기", "재구매율 68%. 왜인지는 후기가 말해줍니다", "/reviews"),
    ]
    for member in campaign.adsets:
        _spread(member, texts, tagged=True)
    return campaign


def _retargeting_campaign() -> Campaign:
    """The control: configured correctly, and the audit should say nothing.

    One ad set, budgeted well above what its 20,000 CPA requires, four
    genuinely different ads, tagged links, a consistent attribution window,
    and a frequency of 4.1 that is high but is not costing response — so the
    fatigue check declines to call it fatigue.
    """
    campaign = Campaign(
        id="demo-c2",
        name="리타게팅 — 장바구니 이탈",
        status="ACTIVE",
        effective_status="ACTIVE",
        objective="OUTCOME_SALES",
        buying_type="AUCTION",
        bid_strategy="LOWEST_COST_WITHOUT_CAP",
        insights=_ins(
            spend=6_000_000, impressions=300_000, clicks=9_000,
            frequency=4.1, purchases=300,
        ),
    )
    adset = AdSet(
        id="demo-c2-as1",
        name="리타겟-장바구니 7일",
        campaign_id=campaign.id,
        status="ACTIVE",
        effective_status="ACTIVE",
        daily_budget=200_000.0,
        optimization_goal="OFFSITE_CONVERSIONS",
        billing_event="IMPRESSIONS",
        promoted_object=dict(PIXEL),
        attribution_spec=[dict(spec) for spec in CLICK_7D],
        targeting=dict(CART_ABANDONERS),
        learning_stage_info={"status": "SUCCESS"},
        insights=_ins(spend=6_000_000, impressions=300_000, clicks=9_000,
                      frequency=4.1, purchases=300),
        prev_insights=_ins(spend=5_600_000, impressions=280_000, clicks=8_120,
                           frequency=3.9, purchases=280),
    )
    campaign.adsets = [adset]
    _spread(
        adset,
        [
            ("장바구니 리마인드", "장바구니에 두고 가신 상품, 아직 있습니다", "/cart"),
            ("재고 경고", "고르신 사이즈 재고가 3개 남았습니다", "/cart"),
            ("무료반품", "마음에 안 들면 무료 반품. 고민은 배송 후에", "/returns"),
            ("후기 모음", "같은 상품을 산 1,240명의 후기", "/reviews"),
        ],
        tagged=True,
    )
    return campaign


def _promotion_campaign() -> Campaign:
    """Ad-set budgets, and most of the tracking problems.

    A sales campaign with one ad set optimising for link clicks, one
    optimising for conversions with no event configured at all, and one that
    Meta has marked learning limited. Their attribution settings differ, so
    even their CPAs are not comparable to each other.
    """
    campaign = Campaign(
        id="demo-c3",
        name="가을 프로모션",
        status="ACTIVE",
        effective_status="ACTIVE",
        objective="OUTCOME_SALES",
        buying_type="AUCTION",
        bid_strategy="LOWEST_COST_WITHOUT_CAP",
        insights=_ins(
            spend=15_000_000, impressions=1_620_000, clicks=66_900,
            frequency=2.3, purchases=112,
        ),
    )

    # Buying the cheapest clicks it can find, inside a sales campaign.
    clicks_adset = AdSet(
        id="demo-c3-as1",
        name="프로모션-클릭최적화",
        campaign_id=campaign.id,
        status="ACTIVE",
        effective_status="ACTIVE",
        daily_budget=300_000.0,
        optimization_goal="LINK_CLICKS",
        billing_event="LINK_CLICKS",
        promoted_object={},
        attribution_spec=[],
        targeting={"geo_locations": {"countries": ["KR"]}, "age_min": 18, "age_max": 65},
        learning_stage_info={"status": "SUCCESS"},
        insights=_ins(spend=9_000_000, impressions=1_200_000, clicks=60_000,
                      frequency=2.4, purchases=100),
        prev_insights=_ins(spend=8_700_000, impressions=1_160_000, clicks=57_000,
                           frequency=2.3, purchases=98),
    )
    # Optimising for conversions with an empty promoted_object: there is no
    # event for Meta to optimise toward, and nothing is recorded.
    blind_adset = AdSet(
        id="demo-c3-as2",
        name="프로모션-전환-신규",
        campaign_id=campaign.id,
        status="ACTIVE",
        effective_status="ACTIVE",
        daily_budget=150_000.0,
        optimization_goal="OFFSITE_CONVERSIONS",
        billing_event="IMPRESSIONS",
        promoted_object={},
        attribution_spec=[dict(spec) for spec in CLICK_7D],
        targeting={"geo_locations": {"countries": ["KR"]}, "age_min": 25, "age_max": 44},
        learning_stage_info={"status": "LEARNING"},
        insights=_ins(spend=4_500_000, impressions=300_000, clicks=4_500,
                      frequency=1.8),
        prev_insights=_ins(spend=1_500_000, impressions=100_000, clicks=1_500,
                           frequency=1.3),
    )
    # Meta's own verdict: learning limited. 12 conversions is also far too
    # few to read a CPA from, which the report says in its own finding.
    limited_adset = AdSet(
        id="demo-c3-as3",
        name="프로모션-리타겟",
        campaign_id=campaign.id,
        status="ACTIVE",
        effective_status="ACTIVE",
        daily_budget=50_000.0,
        optimization_goal="OFFSITE_CONVERSIONS",
        billing_event="IMPRESSIONS",
        promoted_object=dict(PIXEL),
        attribution_spec=[dict(spec) for spec in CLICK_1D],
        targeting={
            "geo_locations": {"countries": ["KR"]},
            "age_min": 20,
            "age_max": 49,
            "custom_audiences": [{"id": "demo-ca-promo", "name": "프로모션 페이지 방문"}],
        },
        learning_stage_info={"status": "FAIL", "conversions": 12},
        insights=_ins(spend=1_500_000, impressions=120_000, clicks=2_400,
                      frequency=2.2, purchases=12),
        prev_insights=_ins(spend=1_450_000, impressions=118_000, clicks=2_350,
                           frequency=2.1, purchases=11),
    )
    campaign.adsets = [clicks_adset, blind_adset, limited_adset]

    _spread(
        clicks_adset,
        [
            ("가을세일 배너", "가을 세일 최대 40%", "/sale/autumn"),
            ("가을세일 영상", "3일간만. 가을 세일 최대 40%", "/sale/autumn"),
        ],
        tagged=False,
    )
    # Four ads, two texts: the apparent creative volume is not real variety.
    _spread(
        blind_adset,
        [
            ("신규A-1", "가을 세일 최대 40%", "/sale/autumn"),
            ("신규A-2", "가을 세일 최대 40%", "/sale/autumn"),
            ("신규B-1", "지금 주문하면 무료배송", "/sale/autumn"),
            ("신규B-2", "지금 주문하면 무료배송", "/sale/autumn"),
        ],
        tagged=False,
    )
    _spread(
        limited_adset,
        [
            ("리타겟-할인", "보고 가신 상품, 지금 40% 입니다", "/sale/autumn"),
            ("리타겟-마감", "세일 마감 48시간 전", "/sale/autumn"),
            ("리타겟-후기", "이번 세일에 가장 많이 팔린 상품", "/sale/autumn"),
        ],
        tagged=True,
    )
    return campaign


def build_demo_account(window_days: int = WINDOW_DAYS) -> Snapshot:
    """The synthetic account, dated so the report reads as a current window."""
    since, until, prev_since, prev_until = _windows(window_days)
    campaigns = [
        _prospecting_campaign(),
        _retargeting_campaign(),
        _promotion_campaign(),
    ]
    return Snapshot(
        account_id=ACCOUNT_ID,
        account_name=ACCOUNT_NAME,
        currency=CURRENCY,
        timezone="Asia/Seoul",
        window_days=window_days,
        since=since,
        until=until,
        prev_since=prev_since,
        prev_until=prev_until,
        campaigns=campaigns,
        account_insights=_ins(
            spend=sum(c.insights.spend for c in campaigns),
            impressions=sum(c.insights.impressions for c in campaigns),
            clicks=sum(c.insights.clicks for c in campaigns),
            frequency=2.9,
            purchases=sum(c.insights.conversions([PURCHASE]) for c in campaigns),
        ),
        # No API call was made to build this, and the report says so.
        api_requests=0,
        warnings=[BANNER],
    )
