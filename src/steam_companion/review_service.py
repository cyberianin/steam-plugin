from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from steam_companion.models import ReviewSummary


class ReviewService:
    """Normalizes aggregate review provider responses into the public contract."""

    def __init__(self, load_summary: Callable[[int, str], Awaitable[dict[str, Any]]]) -> None:
        self._load_summary = load_summary

    async def summary(self, appid: int, review_filter: str) -> ReviewSummary:
        raw = await self._load_summary(appid, review_filter)
        positive = int(raw["positive_reviews"])
        negative = int(raw["negative_reviews"])
        total = positive + negative
        return ReviewSummary(
            appid=appid,
            filter=review_filter,
            review_score=raw.get("review_score"),
            review_score_description=raw.get("review_score_description"),
            total_reviews=int(raw["total_reviews"]),
            positive_reviews=positive,
            negative_reviews=negative,
            positive_percent=round(positive * 100 / total, 1) if total else None,
            provenance={
                "source": "Steamworks appreviews query_summary",
                "provider": "steam_store_reviews",
                "fetched_at": str(raw["fetched_at"]),
                "complete": True,
                "truncated": False,
                "stale": False,
                "warnings": ["Review sentiment reflects the selected filter; review text is not returned."],
            },
        )
