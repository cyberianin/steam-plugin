from __future__ import annotations

from steam_companion.library import LibrarySnapshot
from steam_companion.models import StoreOffer
from steam_companion.storage import Database


class ObservationService:
    """Coordinates durable observations without coupling MCP handlers to storage rules."""

    def __init__(self, db: Database):
        self._db = db

    async def observe_library(self, user_id: str, snapshot: LibrarySnapshot) -> None:
        fetched_at = snapshot.fetched_at
        await self._db.record_library_observation(
            user_id,
            fetched_at[:10],
            snapshot.total,
            sum((game.playtime_forever_minutes or 0) > 0 for game in snapshot),
            sum(game.playtime_forever_minutes or 0 for game in snapshot),
            fetched_at,
            [
                (game.appid, game.playtime_forever_minutes)
                for game in snapshot.items
                if game.playtime_forever_minutes is not None
            ],
        )

    async def observe_store_offers(self, offers: list[StoreOffer], tracked_appids: set[int]) -> None:
        snapshots = [
            (
                offer.appid,
                offer.country_code,
                offer.currency,
                offer.price_state,
                offer.base_price_minor,
                offer.final_price_minor,
                offer.discount_percent,
                offer.fetched_at,
                offer.source,
            )
            for offer in offers
            if (
                offer.appid in tracked_appids
                and offer.price_state in {"priced", "free"}
                and offer.currency
                and offer.final_price_minor is not None
            )
        ]
        if snapshots:
            await self._db.record_price_snapshots(snapshots)
