"""Pull the account into an in-memory snapshot.

Everything the checks need is collected up front so the checks themselves do
no I/O: that makes them pure functions, trivially testable against fixtures,
and keeps the API call count proportional to the account, not to the number
of checks.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .api import GraphClient
from .currency import to_major

log = logging.getLogger("metaaudit.fetch")

ACCOUNT_FIELDS = [
    "id", "name", "account_status", "currency", "timezone_name",
    "amount_spent", "spend_cap", "business", "disable_reason",
]

CAMPAIGN_FIELDS = [
    "id", "name", "status", "effective_status", "objective", "buying_type",
    "daily_budget", "lifetime_budget", "budget_remaining", "bid_strategy",
    "spend_cap", "start_time", "stop_time", "special_ad_categories",
    "created_time", "updated_time",
]

ADSET_FIELDS = [
    "id", "name", "campaign_id", "status", "effective_status",
    "daily_budget", "lifetime_budget", "budget_remaining", "bid_strategy",
    "bid_amount", "optimization_goal", "billing_event", "destination_type",
    "learning_stage_info", "promoted_object", "attribution_spec",
    "targeting", "start_time", "end_time", "created_time", "updated_time",
    "is_dynamic_creative",
]

AD_FIELDS = [
    "id", "name", "adset_id", "campaign_id", "status", "effective_status",
    "created_time", "updated_time",
    "creative{id,name,url_tags,object_story_spec,asset_feed_spec,"
    "effective_object_story_id,degrees_of_freedom_spec,status}",
]

INSIGHT_FIELDS = [
    "spend", "impressions", "clicks", "reach", "frequency", "cpm", "ctr",
    "actions", "action_values", "cost_per_action_type", "purchase_roas",
    "date_start", "date_stop",
]

# Action types that count as "the conversion", most specific first. The
# pixel-scoped ones are preferred because the bare aliases double-count when
# both a pixel and an app event fire.
PURCHASE_ACTIONS = (
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
    "purchase",
)
LEAD_ACTIONS = (
    "offsite_conversion.fb_pixel_lead",
    "onsite_conversion.lead_grouped",
    "omni_lead",
    "lead",
)
# custom_event_type on promoted_object -> the insights action_type to read.
EVENT_TO_ACTIONS: dict[str, tuple[str, ...]] = {
    "PURCHASE": PURCHASE_ACTIONS,
    "LEAD": LEAD_ACTIONS,
    "COMPLETE_REGISTRATION": ("offsite_conversion.fb_pixel_complete_registration",),
    "ADD_TO_CART": ("offsite_conversion.fb_pixel_add_to_cart", "omni_add_to_cart"),
    "INITIATED_CHECKOUT": (
        "offsite_conversion.fb_pixel_initiate_checkout",
        "omni_initiated_checkout",
    ),
    "SUBSCRIBE": ("offsite_conversion.fb_pixel_subscribe",),
    "CONTACT": ("offsite_conversion.fb_pixel_contact",),
}
# Goals that are explicitly not conversions. An ad set on one of these is
# buying traffic, and judging it on purchases is a category error.
NON_CONVERSION_GOALS = {
    "LINK_CLICKS", "LANDING_PAGE_VIEWS", "IMPRESSIONS", "REACH",
    "POST_ENGAGEMENT", "PAGE_LIKES", "THRUPLAY", "VIDEO_VIEWS",
    "AD_RECALL_LIFT",
}


def _num(value: Any) -> float:
    """Graph returns numbers as strings. Missing means zero, not an error."""
    if value in (None, "", []):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    return int(_num(value))


@dataclass
class Insights:
    """One entity's metrics over one window. Money is in major units."""

    spend: float = 0.0
    impressions: int = 0
    clicks: int = 0
    reach: int = 0
    frequency: float = 0.0
    ctr: float = 0.0
    cpm: float = 0.0
    actions: dict[str, float] = field(default_factory=dict)
    action_values: dict[str, float] = field(default_factory=dict)
    date_start: str = ""
    date_stop: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Insights":
        def as_map(key: str) -> dict[str, float]:
            out: dict[str, float] = {}
            for item in row.get(key) or []:
                action_type = item.get("action_type")
                if action_type:
                    out[action_type] = _num(item.get("value"))
            return out

        return cls(
            spend=_num(row.get("spend")),
            impressions=_int(row.get("impressions")),
            clicks=_int(row.get("clicks")),
            reach=_int(row.get("reach")),
            frequency=_num(row.get("frequency")),
            ctr=_num(row.get("ctr")),
            cpm=_num(row.get("cpm")),
            actions=as_map("actions"),
            action_values=as_map("action_values"),
            date_start=row.get("date_start", ""),
            date_stop=row.get("date_stop", ""),
        )

    def conversions(self, action_types: Sequence[str]) -> float:
        """First matching action type wins — summing them double-counts."""
        for action_type in action_types:
            if action_type in self.actions:
                return self.actions[action_type]
        return 0.0

    def revenue(self, action_types: Sequence[str]) -> float:
        for action_type in action_types:
            if action_type in self.action_values:
                return self.action_values[action_type]
        return 0.0


@dataclass
class Ad:
    id: str
    name: str
    adset_id: str
    campaign_id: str
    status: str
    effective_status: str
    creative: dict[str, Any] = field(default_factory=dict)
    updated_time: str = ""
    insights: Insights = field(default_factory=Insights)

    @property
    def is_delivering(self) -> bool:
        return self.effective_status == "ACTIVE"

    def primary_text(self) -> str:
        spec = self.creative.get("object_story_spec") or {}
        for key in ("link_data", "video_data", "photo_data"):
            data = spec.get(key) or {}
            if data.get("message"):
                return str(data["message"])
        feed = self.creative.get("asset_feed_spec") or {}
        bodies = feed.get("bodies") or []
        if bodies and isinstance(bodies[0], dict):
            return str(bodies[0].get("text") or "")
        return ""

    def destination_url(self) -> str:
        spec = self.creative.get("object_story_spec") or {}
        link_data = spec.get("link_data") or {}
        if link_data.get("link"):
            return str(link_data["link"])
        feed = self.creative.get("asset_feed_spec") or {}
        urls = feed.get("link_urls") or []
        if urls and isinstance(urls[0], dict):
            return str(urls[0].get("website_url") or "")
        return ""

    def url_tags(self) -> str:
        return str(self.creative.get("url_tags") or "")


@dataclass
class AdSet:
    id: str
    name: str
    campaign_id: str
    status: str
    effective_status: str
    daily_budget: float = 0.0        # major units, 0 if campaign-budgeted
    lifetime_budget: float = 0.0
    bid_strategy: str = ""
    bid_amount: float = 0.0
    optimization_goal: str = ""
    billing_event: str = ""
    learning_stage_info: dict[str, Any] = field(default_factory=dict)
    promoted_object: dict[str, Any] = field(default_factory=dict)
    attribution_spec: list[dict[str, Any]] = field(default_factory=list)
    targeting: dict[str, Any] = field(default_factory=dict)
    is_dynamic_creative: bool = False
    updated_time: str = ""
    insights: Insights = field(default_factory=Insights)
    prev_insights: Insights = field(default_factory=Insights)
    ads: list[Ad] = field(default_factory=list)

    @property
    def is_delivering(self) -> bool:
        return self.effective_status == "ACTIVE"

    @property
    def learning_status(self) -> str:
        return str(self.learning_stage_info.get("status") or "")

    @property
    def is_learning(self) -> bool:
        return self.learning_status == "LEARNING"

    @property
    def learning_limited(self) -> bool:
        """Meta's term for "will never gather enough events to stabilise"."""
        return self.learning_status == "FAIL"

    @property
    def optimizes_for_conversions(self) -> bool:
        return bool(
            self.optimization_goal
            and self.optimization_goal not in NON_CONVERSION_GOALS
        )

    def conversion_action_types(self) -> tuple[str, ...]:
        event = str(self.promoted_object.get("custom_event_type") or "").upper()
        if event in EVENT_TO_ACTIONS:
            return EVENT_TO_ACTIONS[event]
        if self.optimization_goal == "LEAD_GENERATION":
            return LEAD_ACTIONS
        return PURCHASE_ACTIONS

    def effective_daily_budget(self, campaign: "Campaign") -> float:
        """What this ad set actually gets to spend per day.

        Under campaign budget optimisation the ad set has no budget of its
        own; Meta distributes the campaign budget across its ad sets. An even
        split is the optimistic assumption — real CBO skews, which only makes
        the under-budgeting finding stronger.
        """
        if self.daily_budget:
            return self.daily_budget
        if self.lifetime_budget:
            return 0.0  # handled separately; no meaningful daily figure
        if campaign.daily_budget and campaign.active_adset_count:
            return campaign.daily_budget / campaign.active_adset_count
        return 0.0


@dataclass
class Campaign:
    id: str
    name: str
    status: str
    effective_status: str
    objective: str = ""
    buying_type: str = ""
    daily_budget: float = 0.0
    lifetime_budget: float = 0.0
    bid_strategy: str = ""
    special_ad_categories: list[str] = field(default_factory=list)
    updated_time: str = ""
    insights: Insights = field(default_factory=Insights)
    adsets: list[AdSet] = field(default_factory=list)

    @property
    def is_delivering(self) -> bool:
        return self.effective_status == "ACTIVE"

    @property
    def uses_campaign_budget(self) -> bool:
        return bool(self.daily_budget or self.lifetime_budget)

    @property
    def active_adsets(self) -> list[AdSet]:
        return [a for a in self.adsets if a.is_delivering]

    @property
    def active_adset_count(self) -> int:
        return len(self.active_adsets)


@dataclass
class Snapshot:
    account_id: str
    account_name: str
    currency: str
    timezone: str
    window_days: int
    since: str
    until: str
    prev_since: str
    prev_until: str
    campaigns: list[Campaign] = field(default_factory=list)
    account_insights: Insights = field(default_factory=Insights)
    api_requests: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def adsets(self) -> list[AdSet]:
        return [a for c in self.campaigns for a in c.adsets]

    @property
    def ads(self) -> list[Ad]:
        return [ad for a in self.adsets for ad in a.ads]

    def campaign_of(self, adset: AdSet) -> Campaign | None:
        for campaign in self.campaigns:
            if campaign.id == adset.campaign_id:
                return campaign
        return None


def _windows(window_days: int, today: dt.date | None = None) -> tuple[str, str, str, str]:
    """Current window and the equal-length window immediately before it.

    Ends yesterday: today's numbers are partial, and a partial day dragged
    into a trend comparison manufactures a decline that is not real.
    """
    end = (today or dt.date.today()) - dt.timedelta(days=1)
    start = end - dt.timedelta(days=window_days - 1)
    prev_end = start - dt.timedelta(days=1)
    prev_start = prev_end - dt.timedelta(days=window_days - 1)
    iso = lambda d: d.isoformat()  # noqa: E731
    return iso(start), iso(end), iso(prev_start), iso(prev_end)


def _insights_params(since: str, until: str, level: str) -> dict[str, Any]:
    return {
        "level": level,
        "fields": ",".join(INSIGHT_FIELDS),
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "time_increment": "all_days",
        "limit": 500,
    }


def _index_insights(
    client: GraphClient, account: str, since: str, until: str, level: str
) -> dict[str, Insights]:
    """One insights call per level, keyed by entity id — not one call per entity."""
    id_key = {"campaign": "campaign_id", "adset": "adset_id", "ad": "ad_id"}[level]
    params = _insights_params(since, until, level)
    params["fields"] = params["fields"] + f",{id_key}"
    out: dict[str, Insights] = {}
    for row in client.paginate(f"{account}/insights", params):
        entity_id = row.get(id_key)
        if entity_id:
            out[str(entity_id)] = Insights.from_row(row)
    return out


def fetch_snapshot(
    client: GraphClient,
    account_id: str,
    *,
    window_days: int = 30,
    include_paused: bool = False,
    today: dt.date | None = None,
) -> Snapshot:
    since, until, prev_since, prev_until = _windows(window_days, today)

    account = client.get(account_id, {"fields": ",".join(ACCOUNT_FIELDS)})
    currency = account.get("currency", "USD")
    snap = Snapshot(
        account_id=account_id,
        account_name=account.get("name", account_id),
        currency=currency,
        timezone=account.get("timezone_name", ""),
        window_days=window_days,
        since=since,
        until=until,
        prev_since=prev_since,
        prev_until=prev_until,
    )

    # Effective-status filter, applied server side so we do not page through
    # years of archived campaigns to throw them away locally.
    live = ["ACTIVE", "PAUSED"] if include_paused else ["ACTIVE"]
    status_filter = (
        '[{"field":"effective_status","operator":"IN","value":'
        + "[" + ",".join(f'"{s}"' for s in live) + "]}]"
    )

    log.info("fetching campaigns")
    campaigns: dict[str, Campaign] = {}
    for row in client.paginate(
        f"{account_id}/campaigns",
        {"fields": ",".join(CAMPAIGN_FIELDS), "filtering": status_filter},
    ):
        campaigns[row["id"]] = Campaign(
            id=row["id"],
            name=row.get("name", ""),
            status=row.get("status", ""),
            effective_status=row.get("effective_status", ""),
            objective=row.get("objective", ""),
            buying_type=row.get("buying_type", ""),
            daily_budget=to_major(row.get("daily_budget"), currency),
            lifetime_budget=to_major(row.get("lifetime_budget"), currency),
            bid_strategy=row.get("bid_strategy", "") or "",
            special_ad_categories=row.get("special_ad_categories") or [],
            updated_time=row.get("updated_time", ""),
        )

    log.info("fetching ad sets")
    adsets: dict[str, AdSet] = {}
    for row in client.paginate(
        f"{account_id}/adsets",
        {"fields": ",".join(ADSET_FIELDS), "filtering": status_filter},
    ):
        adset = AdSet(
            id=row["id"],
            name=row.get("name", ""),
            campaign_id=row.get("campaign_id", ""),
            status=row.get("status", ""),
            effective_status=row.get("effective_status", ""),
            daily_budget=to_major(row.get("daily_budget"), currency),
            lifetime_budget=to_major(row.get("lifetime_budget"), currency),
            bid_strategy=row.get("bid_strategy", "") or "",
            bid_amount=to_major(row.get("bid_amount"), currency),
            optimization_goal=row.get("optimization_goal", "") or "",
            billing_event=row.get("billing_event", "") or "",
            learning_stage_info=row.get("learning_stage_info") or {},
            promoted_object=row.get("promoted_object") or {},
            attribution_spec=row.get("attribution_spec") or [],
            targeting=row.get("targeting") or {},
            is_dynamic_creative=bool(row.get("is_dynamic_creative")),
            updated_time=row.get("updated_time", ""),
        )
        adsets[adset.id] = adset
        parent = campaigns.get(adset.campaign_id)
        if parent is not None:
            parent.adsets.append(adset)
        else:
            snap.warnings.append(
                f"ad set {adset.id} references campaign {adset.campaign_id}, "
                "which is not in the fetched set (likely filtered out by status)"
            )

    log.info("fetching ads and creatives")
    for row in client.paginate(
        f"{account_id}/ads",
        {"fields": ",".join(AD_FIELDS), "filtering": status_filter},
    ):
        ad = Ad(
            id=row["id"],
            name=row.get("name", ""),
            adset_id=row.get("adset_id", ""),
            campaign_id=row.get("campaign_id", ""),
            status=row.get("status", ""),
            effective_status=row.get("effective_status", ""),
            creative=row.get("creative") or {},
            updated_time=row.get("updated_time", ""),
        )
        owner = adsets.get(ad.adset_id)
        if owner is not None:
            owner.ads.append(ad)

    log.info("fetching insights (%s to %s)", since, until)
    account_rows = list(
        client.paginate(f"{account_id}/insights", _insights_params(since, until, "account"))
    )
    if account_rows:
        snap.account_insights = Insights.from_row(account_rows[0])

    for entity_id, ins in _index_insights(client, account_id, since, until, "campaign").items():
        if entity_id in campaigns:
            campaigns[entity_id].insights = ins
    for entity_id, ins in _index_insights(client, account_id, since, until, "adset").items():
        if entity_id in adsets:
            adsets[entity_id].insights = ins
    ad_index = {ad.id: ad for ad in (a for s in adsets.values() for a in s.ads)}
    for entity_id, ins in _index_insights(client, account_id, since, until, "ad").items():
        if entity_id in ad_index:
            ad_index[entity_id].insights = ins

    log.info("fetching previous-window ad set insights (%s to %s)", prev_since, prev_until)
    for entity_id, ins in _index_insights(
        client, account_id, prev_since, prev_until, "adset"
    ).items():
        if entity_id in adsets:
            adsets[entity_id].prev_insights = ins

    snap.campaigns = list(campaigns.values())
    snap.api_requests = client.request_count
    return snap
