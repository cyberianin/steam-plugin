from __future__ import annotations

from steam_companion.models import PriceHistory, PriceHistoryPoint
from steam_companion.request_context import UserContext
from steam_companion.storage import Database
from steam_companion.steam import utc_now_iso


class PriceHistoryService:
    def __init__(self, db: Database):
        self._db = db

    async def read(self, user: UserContext, appid: int, limit: int) -> PriceHistory:
        tracked = await self._db.get_price_tracking(user.user_id)
        tracked_appids = {int(row["appid"]) for row in tracked if row.get("appid") is not None}
        if appid in tracked_appids:
            rows = await self._db.get_price_snapshots(appid, user.country_code, user.expected_currency, limit)
        else:
            rows = []
        points = [PriceHistoryPoint.model_validate(row) for row in reversed(rows)]
        warnings = [
            "History begins when this service first observes a price; it is not Steam's historical price feed.",
            "Price observations use the unofficial Steam Storefront appdetails interface.",
        ]
        if appid not in tracked_appids:
            warnings.append("Price tracking is not enabled for this AppID; enable it on the local backlog page.")
        return PriceHistory(
            appid=appid, country_code=user.country_code, currency=user.expected_currency,
            points=points, returned=len(points),
            provenance={"source": "local SQLite snapshots of observed Steam Storefront appdetails prices",
                        "provider": "steam_companion_database", "fetched_at": utc_now_iso(), "complete": True,
                        "truncated": len(rows) >= limit, "stale": False, "warnings": warnings},
        )
