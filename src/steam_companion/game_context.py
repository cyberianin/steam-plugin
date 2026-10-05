from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from steam_companion.library import LibrarySnapshot
from steam_companion.models import GameContext, ReviewSummary, StoreOffer
from steam_companion.request_context import UserContext

LibraryLoader = Callable[[str, bool], Awaitable[tuple[LibrarySnapshot, int, str]]]
OfferLoader = Callable[[int, UserContext, bool], Awaitable[StoreOffer]]
MetadataLoader = Callable[[int, UserContext, bool], Awaitable[Any]]
AchievementLoader = Callable[[str, int, str], Awaitable[dict[str, Any]]]
ReviewLoader = Callable[[int, str], Awaitable[dict[str, Any]]]
PlayerLoader = Callable[[int], Awaitable[dict[str, Any]]]


class GameContextService:
    def __init__(
        self,
        load_library: LibraryLoader,
        load_offer: OfferLoader,
        load_metadata: MetadataLoader,
        load_achievements: AchievementLoader,
        load_reviews: ReviewLoader,
        load_players: PlayerLoader,
    ) -> None:
        self._load_library = load_library
        self._load_offer = load_offer
        self._load_metadata = load_metadata
        self._load_achievements = load_achievements
        self._load_reviews = load_reviews
        self._load_players = load_players

    async def build(self, appid: int, user: UserContext, *, fresh: bool = False) -> GameContext:
        warnings: list[str] = []

        async def safe(label: str, awaitable: Awaitable[Any]) -> Any:
            try:
                return await awaitable
            except Exception as exc:
                warnings.append(f"{label}:{getattr(exc, 'code', type(exc).__name__)}")
                return None

        async def progress() -> dict[str, int | float | None]:
            result = await self._load_achievements(user.steam_id, appid, user.preferred_language)
            definitions = result.get("definitions", [])
            players = result.get("player_achievements", [])
            unlocked = {
                item.get("apiname") for item in players if isinstance(item, dict)
                and item.get("achieved") and isinstance(item.get("apiname"), str)
            }
            names = [item.get("name") for item in definitions
                     if isinstance(item, dict) and isinstance(item.get("name"), str)]
            count = sum(name in unlocked for name in names)
            return {"unlocked": count, "total": len(names),
                    "completion_percent": round(count * 100 / len(names), 1) if names else None}

        results = await asyncio.gather(
            safe("ownership", self._load_library(user.steam_id, fresh)),
            safe("offer", self._load_offer(appid, user, fresh)),
            safe("metadata", self._load_metadata(appid, user, fresh)),
            safe("achievements", progress()),
            safe("lifetime_reviews", self._load_reviews(appid, "all")),
            safe("recent_reviews", self._load_reviews(appid, "recent")),
            safe("current_players", self._load_players(appid)),
        )
        library_result, offer, metadata, achievements, lifetime, recent, players = results
        library = library_result[0] if library_result is not None else None
        fetched_at = library_result[2] if library_result is not None else ""
        game = library.by_appid.get(appid) if library else None
        ownership = (
            "owned" if game else
            "unknown" if library is None or offer is None or offer.price_state != "priced" else
            "not_owned"
        )

        def review_model(filter_name: str, raw: Any) -> ReviewSummary | None:
            if not isinstance(raw, dict):
                return None
            positive, negative, total = (raw.get("positive_reviews"), raw.get("negative_reviews"),
                                         raw.get("total_reviews"))
            if not all(isinstance(value, int) and not isinstance(value, bool)
                       for value in (positive, negative, total)):
                return None
            return ReviewSummary(
                appid=appid, filter=filter_name, review_score=raw.get("review_score"),
                review_score_description=raw.get("review_score_description"), total_reviews=total,
                positive_reviews=positive, negative_reviews=negative,
                positive_percent=round(positive * 100 / total, 1) if total else None,
                provenance={"source": "Steamworks appreviews query_summary", "provider": "steam_appreviews",
                            "fetched_at": str(raw.get("fetched_at", "")), "complete": True,
                            "truncated": False, "stale": False, "warnings": ["Review text is not returned."]},
            )

        return GameContext(
            appid=appid, name=game.name if game else offer.name if offer else metadata.name if metadata else None,
            ownership=ownership, playtime_forever_minutes=game.playtime_forever_minutes if game else None,
            last_played_at=game.last_played_at if game else None, offer=offer, metadata=metadata,
            achievement_progress=achievements, lifetime_reviews=review_model("all", lifetime),
            recent_reviews=review_model("recent", recent),
            current_players=players.get("player_count") if isinstance(players, dict) else None,
            provenance={"source": "Steam Web API, Steam Storefront appdetails and aggregate review endpoints",
                        "provider": "steam_web_api+steam_storefront_appdetails+steam_appreviews",
                        "fetched_at": fetched_at, "complete": not warnings and offer is not None
                        and offer.price_state != "provider_error", "truncated": False, "stale": False,
                        "warnings": ["Storefront metadata and offers use Steam's unofficial appdetails interface.",
                                     *warnings]},
        )
