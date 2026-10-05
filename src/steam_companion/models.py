from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Provenance(StrictModel):
    source: str
    provider: str
    fetched_at: str
    complete: bool = True
    truncated: bool = False
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class GameRecord(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    appid: int
    name: str | None = None
    playtime_forever_minutes: int | None = None
    playtime_minutes: int | None = None
    playtime_hours: float | None = None
    playtime_2weeks_minutes: int | None = None
    last_played_at: str | None = None
    icon_url: str | None = None
    platform_playtime_minutes: dict[str, int] = Field(default_factory=dict)
    ownership: Literal["owned", "unknown"] = "owned"
    owned: bool | None = True
    source: str = "steam_web_api"


class LibraryPage(StrictModel):
    total_matching: int
    returned: int
    complete: bool
    next_cursor: str | None
    data_fetched_at: str
    games: list[GameRecord]
    provenance: Provenance


class LibrarySummary(StrictModel):
    total_games: int
    played_games: int
    unplayed_games: int
    total_playtime_minutes: int
    total_playtime_hours: float | None = None
    recently_played_games: int | None = None
    top_played: list[GameRecord]
    provenance: Provenance


class LibraryAnalysis(StrictModel):
    total_games: int
    played_games: int
    unplayed_games: int
    total_playtime_minutes: int
    median_playtime_minutes: float | None
    playtime_p25_minutes: float | None
    playtime_p75_minutes: float | None
    recently_active: list[GameRecord]
    dormant: list[GameRecord]
    abandoned: list[GameRecord]
    classification_basis: dict[str, str | int | bool]
    provenance: Provenance


class LibraryHistoryPoint(StrictModel):
    snapshot_date: str
    total_games: int
    played_games: int
    total_playtime_minutes: int
    fetched_at: str


class LibraryHistory(StrictModel):
    points: list[LibraryHistoryPoint]
    returned: int
    source: str
    provenance: Provenance


class ActivityDelta(StrictModel):
    appid: int
    delta_minutes: int
    previous_playtime_minutes: int
    current_playtime_minutes: int
    observed_at: str


class ActivityHistory(StrictModel):
    points: list[ActivityDelta]
    returned: int
    provenance: Provenance


class ObservedEvent(StrictModel):
    event_type: Literal[
        "game_acquired", "game_first_played", "playtime_changed", "achievement_unlocked",
        "wishlist_added", "wishlist_removed", "sale_started", "price_changed", "new_personal_observed_low",
    ]
    appid: int
    observed_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EventHistory(StrictModel):
    events: list[ObservedEvent]
    returned: int
    provenance: Provenance


class PriceHistoryPoint(StrictModel):
    appid: int
    country_code: str
    currency: str
    price_state: Literal["priced", "free"]
    base_price_minor: int | None
    final_price_minor: int
    discount_percent: int | None
    fetched_at: str
    source: str


class PriceHistory(StrictModel):
    appid: int
    country_code: str
    currency: str
    points: list[PriceHistoryPoint]
    returned: int
    provenance: Provenance


class ReviewSummary(StrictModel):
    appid: int
    filter: Literal["all", "recent"]
    review_score: int | None
    review_score_description: str | None
    total_reviews: int
    positive_reviews: int
    negative_reviews: int
    positive_percent: float | None
    provenance: Provenance


class FriendSharedGame(StrictModel):
    appid: int
    name: str | None


class FriendCoopMatch(StrictModel):
    persona_name: str | None
    profile_url: str | None
    shared_games: list[FriendSharedGame]


class CoopContext(StrictModel):
    friends_total: int
    friends_checked: int
    friends_with_public_libraries: int
    matches: list[FriendCoopMatch]
    provenance: Provenance


class GameContext(StrictModel):
    appid: int
    name: str | None
    ownership: Literal["owned", "not_owned", "unknown"]
    playtime_forever_minutes: int | None
    last_played_at: str | None
    offer: StoreOffer | None
    metadata: GameMetadata | None = None
    achievement_progress: dict[str, int | float | None] | None = None
    lifetime_reviews: ReviewSummary | None = None
    recent_reviews: ReviewSummary | None = None
    current_players: int | None = None
    provenance: Provenance


class GameMetadata(StrictModel):
    appid: int
    name: str | None = None
    game_type: str | None = None
    release_date: str | None = None
    coming_soon: bool | None = None
    developers: list[str] = Field(default_factory=list)
    publishers: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    controller_support: str | None = None
    coop: bool | None = None
    multiplayer: bool | None = None
    steam_deck: str | None = None
    languages: list[str] = Field(default_factory=list)
    dlc_appids: list[int] = Field(default_factory=list)
    package_ids: list[int] = Field(default_factory=list)
    metacritic_score: int | None = None
    provenance: Provenance


class PackageItem(StrictModel):
    appid: int
    name: str | None = None
    owned: bool | None = None
    ownership: Literal["owned", "not_owned", "unknown"] = "unknown"


class PackageAnalysis(StrictModel):
    package_id: int
    name: str | None = None
    price_state: Literal["priced", "missing_price"]
    currency: str | None = None
    initial_price_minor: int | None = None
    final_price_minor: int | None = None
    discount_percent: int | None = None
    apps: list[PackageItem] = Field(default_factory=list)
    owned_appids: list[int] = Field(default_factory=list)
    duplicate_owned_count: int = 0
    included_app_count: int = 0
    known_owned_count: int = 0
    unknown_item_count: int = 0
    confirmed_coverage_percent: float | None = None
    fully_covered: bool | None = None
    source: str
    source_stability: Literal["unofficial"]
    fetched_at: str
    warnings: list[str] = Field(default_factory=list)


class GameComparisonItem(StrictModel):
    appid: int
    name: str | None
    ownership: Literal["owned", "not_owned", "unknown"]
    playtime_minutes: int | None
    last_played_at: str | None
    price_state: str
    final_price_minor: int | None
    currency: str | None
    discount_percent: int | None
    store_url: str


class GameComparison(StrictModel):
    games: list[GameComparisonItem]
    returned: int
    provenance: Provenance


class RecentGames(StrictModel):
    games: list[GameRecord]
    returned: int
    provenance: Provenance


class AchievementItem(StrictModel):
    api_name: str
    display_name: str | None = None
    description: str | None = None
    unlocked: bool
    unlocked_at: str | None = None


class AchievementSummary(StrictModel):
    appid: int
    game_name: str | None
    total: int
    unlocked: int
    locked: int
    completion_percent: float | None
    recently_unlocked: list[AchievementItem]
    provenance: Provenance


class WishlistItem(StrictModel):
    appid: int
    priority: int | None = None
    added_at: str | None = None


class WishlistPage(StrictModel):
    total: int
    returned: int
    next_cursor: str | None
    items: list[WishlistItem]
    provenance: Provenance


class WishlistSummary(StrictModel):
    total: int
    priority_order: list[WishlistItem]
    recently_added: list[WishlistItem]
    provenance: Provenance


class OwnershipResult(StrictModel):
    appid: int
    ownership: Literal["owned", "not_owned", "unknown"]
    owned: bool | None = None
    playtime_minutes: int | None = None
    last_played_at: str | None = None
    name: str | None = None
    reason: str


class SteamAccount(StrictModel):
    steam_id: str
    persona_name: str | None
    profile_url: str | None
    avatar: str | None
    created_at: int | None
    visibility_state: int | None
    steam_level: int | None
    store_country_code: str
    preferred_language: str
    expected_currency: str
    provenance: Provenance


class StoreOffer(StrictModel):
    appid: int
    name: str | None = None
    country_code: str
    currency: str | None = None
    base_price_minor: int | None = None
    final_price_minor: int | None = None
    discount_percent: int | None = None
    price_state: Literal[
        "priced", "free", "not_for_sale", "not_available_in_region", "unreleased", "missing_price", "provider_error"
    ]
    currency_mismatch: bool = False
    store_url: str
    fetched_at: str
    source: str
    source_stability: Literal["official", "unofficial"]
    error: str | None = None


class WalletState(StrictModel):
    available: bool
    amount_minor: int | None
    currency: str | None
    formatted: str | None
    updated_at: str | None
    age_seconds: int | None
    source: str
    live: bool
    stale: bool


class Capabilities(StrictModel):
    schema_version: str
    steam_identity: dict
    steam_account: dict
    library: dict
    recent_games: dict
    activity_history: dict
    achievements: dict
    wishlist: dict
    reviews: dict
    social: dict
    store_prices: dict
    wallet: dict
    local_sidecar: dict


class PurchaseItem(StrictModel):
    appid: int = Field(gt=0, le=2_147_483_647)
    ownership: Literal["owned", "not_owned", "unknown"]
    offer: StoreOffer
    can_afford: bool | None
    remaining_minor: int | None


class PurchaseContext(StrictModel):
    items: list[PurchaseItem]
    wallet: WalletState
    currency: str | None
    steam_wishlist_appids: list[int] = Field(default_factory=list)
    local_watchlist_appids: list[int] = Field(default_factory=list)
    price_tracking_appids: list[int] = Field(default_factory=list)
    recent_price_history: dict[int, list[PriceHistoryPoint]] = Field(default_factory=dict)
    complete: bool
    warnings: list[str]
