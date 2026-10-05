from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from steam_companion.errors import ServiceError
from steam_companion.library import LibrarySnapshot
from steam_companion.models import GameComparison, GameComparisonItem, StoreOffer
from steam_companion.request_context import UserContext

LibraryLoader = Callable[[str], Awaitable[tuple[LibrarySnapshot, int, str]]]
OfferLoader = Callable[[list[int], UserContext], Awaitable[list[StoreOffer]]]


class GameComparisonService:
    def __init__(self, load_library: LibraryLoader, load_offers: OfferLoader):
        self._load_library = load_library
        self._load_offers = load_offers

    async def compare(self, appids: list[int], user: UserContext) -> GameComparison:
        if not 2 <= len(appids) <= 5 or len(set(appids)) != len(appids):
            raise ServiceError("invalid_appid", "Provide two to five unique AppIDs.", 400)
        library_result, offer_result = await asyncio.gather(
            self._load_library(user.steam_id), self._load_offers(appids, user), return_exceptions=True
        )
        if isinstance(library_result, BaseException) and isinstance(offer_result, BaseException):
            raise library_result
        library = None if isinstance(library_result, BaseException) else library_result[0]
        fetched_at = (
            library_result[2] if not isinstance(library_result, BaseException) else ""
        )
        if isinstance(offer_result, BaseException):
            offers = [None] * len(appids)
            offer_warning = "store_unavailable"
        else:
            offers = offer_result
            offer_warning = None
        comparison: list[GameComparisonItem] = []
        warnings: list[str] = []
        for appid, offer in zip(appids, offers, strict=True):
            game = library.by_appid.get(appid) if library else None
            ownership = "owned" if game else "unknown"
            if offer is None:
                warnings.append(f"store_unavailable:{appid}")
                comparison.append(GameComparisonItem(
                    appid=appid, name=game.name if game else None, ownership=ownership,
                    playtime_minutes=game.playtime_minutes if game else None,
                    last_played_at=game.last_played_at if game else None,
                    price_state="provider_error", final_price_minor=None, currency=None,
                    discount_percent=None, store_url=f"https://store.steampowered.com/app/{appid}/",
                ))
                continue
            if not game and offer.price_state in {"free", "provider_error", "missing_price"}:
                ownership = "unknown"
            elif not game and library is None:
                ownership = "unknown"
            else:
                ownership = "owned" if game else "not_owned"
            comparison.append(GameComparisonItem(
                appid=appid, name=(game.name if game else None) or offer.name, ownership=ownership,
                playtime_minutes=game.playtime_minutes if game else None,
                last_played_at=game.last_played_at if game else None,
                price_state=offer.price_state, final_price_minor=offer.final_price_minor,
                currency=offer.currency, discount_percent=offer.discount_percent,
                store_url=offer.store_url,
            ))
            if offer.price_state == "provider_error":
                warnings.append(f"store_unavailable:{appid}")
        if isinstance(library_result, BaseException):
            warnings.append("library_unavailable")
        return GameComparison(
            games=comparison, returned=len(comparison),
            provenance={
                "source": "Steam Web API library and regional Storefront offers",
                "provider": "steam_web_api+steam_storefront",
                "fetched_at": fetched_at,
                "complete": not warnings and offer_warning is None,
                "truncated": False, "stale": False, "warnings": sorted(set(warnings)),
            },
        )
