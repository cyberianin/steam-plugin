from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from steam_companion.models import WishlistItem, WishlistPage, WishlistSummary
from steam_companion.pagination import SnapshotCursor
from steam_companion.steam import utc_now_iso

WishlistLoader = Callable[[str], Awaitable[dict[str, Any]]]
ObservationWriter = Callable[[str, list[int], str], Awaitable[None]]


class WishlistService:
    def __init__(self, load: WishlistLoader, observe: ObservationWriter, cursor_secret: bytes):
        self._load = load
        self._observe = observe
        self._cursor_secret = cursor_secret

    async def observe_snapshot(self, user_id: str, steam_id: str) -> tuple[list[WishlistItem], str]:
        """Fetch, normalize and explicitly persist one complete wishlist observation."""
        result = await self._load(steam_id)
        items: list[WishlistItem] = []
        for item in result["items"]:
            added = item.get("date_added")
            items.append(WishlistItem(
                appid=int(item["appid"]), priority=item.get("priority"),
                added_at=(datetime.fromtimestamp(added, UTC).isoformat().replace("+00:00", "Z")
                          if isinstance(added, int) and added > 0 else None),
            ))
        fetched_at = str(result.get("_service_fetched_at", utc_now_iso()))
        await self._observe(user_id, [item.appid for item in items], fetched_at)
        return items, fetched_at

    def summary(self, items: list[WishlistItem], fetched_at: str) -> WishlistSummary:
        return WishlistSummary(
            total=len(items),
            priority_order=sorted(items, key=lambda item: (item.priority is None, item.priority or 0))[:10],
            recently_added=sorted(items, key=lambda item: item.added_at or "", reverse=True)[:10],
            provenance={"source": "Steam Web API IWishlistService.GetWishlist", "provider": "steam_web_api",
                        "fetched_at": fetched_at, "complete": True, "truncated": len(items) > 10,
                        "stale": False,
                        "warnings": ["Wishlist endpoint is available but has limited static Steam documentation."]},
        )

    async def observe_summary(self, user_id: str, steam_id: str) -> WishlistSummary:
        items, fetched_at = await self.observe_snapshot(user_id, steam_id)
        return self.summary(items, fetched_at)

    async def observe_page(
        self, user_id: str, steam_id: str, cursor: str | None, limit: int
    ) -> WishlistPage:
        items, fetched_at = await self.observe_snapshot(user_id, steam_id)
        return self.page(user_id, items, fetched_at, cursor, limit)

    def page(
        self, user_id: str, items: list[WishlistItem], fetched_at: str,
        cursor: str | None, limit: int,
    ) -> WishlistPage:
        principal = hashlib.sha256(user_id.encode()).hexdigest()[:24]
        filters: dict[str, str] = {}
        offset = SnapshotCursor.decode(cursor, principal, filters, self._cursor_secret, fetched_at, "wishlist") if cursor else 0
        page = items[offset:offset + limit]
        next_cursor = (SnapshotCursor.encode(offset + len(page), principal, filters, self._cursor_secret,
                                             fetched_at, "wishlist")
                       if offset + len(page) < len(items) else None)
        return WishlistPage(
            total=len(items), returned=len(page), next_cursor=next_cursor, items=page,
            provenance={"source": "Steam Web API IWishlistService.GetWishlist", "provider": "steam_web_api",
                        "fetched_at": fetched_at, "complete": True, "truncated": next_cursor is not None,
                        "stale": False, "warnings": ["Cursor is bound to this wishlist snapshot."]},
        )
