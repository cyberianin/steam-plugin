from __future__ import annotations

from collections.abc import Awaitable, Callable

from steam_companion.errors import ServiceError
from steam_companion.models import StoreOffer
from steam_companion.observations import ObservationService
from steam_companion.request_context import UserContext
from steam_companion.store import StoreProvider
from steam_companion.storage import Database


class StorefrontService:
    """Owns regional offer reads and the explicit tracked-price observation path."""

    def __init__(
        self,
        store: StoreProvider,
        db: Database,
        observations: ObservationService,
        current_user_id: Callable[[], str],
        load_user_context: Callable[[], Awaitable[UserContext]],
    ) -> None:
        self._store = store
        self._db = db
        self._observations = observations
        self._current_user_id = current_user_id
        self._load_user_context = load_user_context

    async def offers(
        self,
        appids: list[int],
        *,
        user_context: UserContext | None = None,
        fresh: bool = False,
    ) -> tuple[list[StoreOffer], UserContext]:
        self._validate_appids(appids)
        context = user_context or await self._load_user_context()
        offers = await self._store.get_offers(
            appids,
            context.country_code,
            context.preferred_language,
            context.expected_currency,
            fresh=fresh,
        )
        return offers, context

    async def observe_offers(
        self,
        appids: list[int],
        *,
        user_context: UserContext | None = None,
        fresh: bool = False,
        tracked_appids: set[int] | None = None,
    ) -> tuple[list[StoreOffer], UserContext]:
        """Fetch offers and persist eligible prices for currently tracked AppIDs."""
        offers, context = await self.offers(appids, user_context=user_context, fresh=fresh)
        if tracked_appids is None:
            tracked_rows = await self._db.get_price_tracking(self._current_user_id())
            tracked_appids = {
                int(row["appid"])
                for row in tracked_rows
                if row.get("appid") is not None
            }
        await self._observations.observe_store_offers(offers, tracked_appids)
        return offers, context

    @staticmethod
    def _validate_appids(appids: list[int]) -> None:
        if not appids or len(appids) > 50 or any(appid <= 0 for appid in appids):
            raise ServiceError("invalid_appid", "Provide between 1 and 50 positive AppIDs.", 400)
        if len(set(appids)) != len(appids):
            raise ServiceError("invalid_appid", "AppIDs must be unique.", 400)
