from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping

from steam_companion.library import LibrarySnapshot
from steam_companion.models import GameRecord, PriceHistoryPoint, PurchaseContext, PurchaseItem, StoreOffer, WishlistItem
from steam_companion.request_context import UserContext
from steam_companion.storage import Database
from steam_companion.wallet import WalletProvider

LibraryLoader = Callable[..., Awaitable[tuple[LibrarySnapshot, int, str]]]
OfferLoader = Callable[..., Awaitable[tuple[list[StoreOffer], UserContext]]]
WishlistLoader = Callable[..., Awaitable[tuple[list[WishlistItem], str]]]


class PurchaseContextService:
    """Joins independent account, store and local-state reads for purchase decisions."""

    def __init__(
        self,
        db: Database,
        wallet: WalletProvider,
        load_library: LibraryLoader,
        load_offers: OfferLoader,
        load_wishlist: WishlistLoader,
    ) -> None:
        self._db = db
        self._wallet = wallet
        self._load_library = load_library
        self._load_offers = load_offers
        self._load_wishlist = load_wishlist

    async def build(
        self, user_context: UserContext, appids: list[int], *, wallet_settings: Mapping[str, object]
    ) -> PurchaseContext:
        warnings: list[str] = []
        try:
            tracking_result = await self._db.get_price_tracking(user_context.user_id)
            price_tracking_appids = [int(row["appid"]) for row in tracking_result if row.get("appid") is not None]
            tracking_failed = False
        except Exception:
            price_tracking_appids = []
            tracking_failed = True
            warnings.append("price_tracking_state_unavailable")
        tracked_appids = set(price_tracking_appids)

        library_result, offers_result, wishlist_result, watchlist_result = await asyncio.gather(
            self._load_library(steam_id=user_context.steam_id),
            self._load_offers(
                appids,
                user_context=user_context,
                tracked_appids=tracked_appids,
            ),
            self._load_wishlist(user_context.steam_id),
            self._db.get_local_watchlist(user_context.user_id),
            return_exceptions=True,
        )
        if isinstance(library_result, Exception):
            games: Iterable[GameRecord] = ()
            warnings.append("ownership_source_unavailable")
        else:
            games, _, _ = library_result

        if isinstance(offers_result, Exception):
            offers = [
                StoreOffer(
                    appid=appid,
                    country_code=user_context.country_code,
                    price_state="provider_error",
                    source="steam_storefront_appdetails",
                    source_stability="unofficial",
                    fetched_at=self._now(),
                    store_url=f"https://store.steampowered.com/app/{appid}/",
                    error="store_unavailable",
                )
                for appid in appids
            ]
            warnings.append("offer_source_unavailable")
        else:
            offers, _ = offers_result

        if isinstance(wishlist_result, Exception):
            steam_wishlist_appids: list[int] = []
            warnings.append("steam_wishlist_unavailable")
        else:
            wishlist, _ = wishlist_result
            steam_wishlist_appids = [item.appid for item in wishlist]

        if isinstance(watchlist_result, Exception):
            local_watchlist_appids: list[int] = []
            warnings.append("local_watchlist_unavailable")
        else:
            local_watchlist_appids = [int(row["appid"]) for row in watchlist_result if row.get("appid") is not None]

        wallet_state = await self._wallet.get_state(wallet_settings)
        tracked_candidates = [appid for appid in appids if appid in tracked_appids]
        try:
            price_rows = await self._db.get_price_snapshots_batch(
                tracked_candidates, user_context.country_code, user_context.expected_currency, 5
            )
        except Exception:
            price_rows = {}
            warnings.append("local_price_history_unavailable")
        recent_price_history = {
            appid: [PriceHistoryPoint.model_validate(row) for row in rows]
            for appid, rows in price_rows.items()
            if rows
        }

        owned = {game.appid for game in games}
        items: list[PurchaseItem] = []
        for appid, offer in zip(appids, offers, strict=True):
            is_owned = appid in owned
            ownership = (
                "owned" if is_owned else
                "unknown" if isinstance(library_result, Exception) else
                "not_owned" if offer.price_state == "priced" else "unknown"
            )
            if ownership == "unknown":
                warnings.append(f"ownership_unknown:{appid}")
            can_afford: bool | None = None
            remaining: int | None = None
            if (
                not is_owned
                and ownership == "not_owned"
                and wallet_state.available
                and not wallet_state.stale
                and offer.price_state in {"priced", "free"}
                and not offer.currency_mismatch
                and offer.currency == wallet_state.currency
                and offer.final_price_minor is not None
            ):
                remaining = int(wallet_state.amount_minor) - offer.final_price_minor
                can_afford = remaining >= 0
            elif offer.currency_mismatch:
                warnings.append(f"currency_mismatch:{appid}")
            items.append(PurchaseItem(
                appid=appid,
                ownership=ownership,
                offer=offer,
                can_afford=can_afford,
                remaining_minor=remaining,
            ))

        return PurchaseContext(
            items=items,
            wallet=wallet_state,
            currency=wallet_state.currency if wallet_state.available else None,
            steam_wishlist_appids=steam_wishlist_appids,
            local_watchlist_appids=local_watchlist_appids,
            price_tracking_appids=price_tracking_appids,
            recent_price_history=recent_price_history,
            complete=(
                not tracking_failed
                and not isinstance(library_result, Exception)
                and not isinstance(offers_result, Exception)
                and not isinstance(wishlist_result, Exception)
                and not isinstance(watchlist_result, Exception)
                and not any(item.offer.price_state == "provider_error" for item in items)
            ),
            warnings=sorted(set(warnings)),
        )

    @staticmethod
    def _now() -> str:
        from steam_companion.steam import utc_now_iso

        return utc_now_iso()
