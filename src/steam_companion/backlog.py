from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from steam_companion.library import LibrarySnapshot
from steam_companion.models import GameRecord
from steam_companion.request_context import UserContext

RowsLoader = Callable[[str], Awaitable[list[dict[str, Any]]]]
LibraryLoader = Callable[[str], Awaitable[tuple[LibrarySnapshot, int, str]]]
AchievementLoader = Callable[[str, int, str], Awaitable[dict[str, Any]]]


class BacklogService:
    """Combines declared local state with clearly labeled library-derived suggestions."""

    def __init__(
        self,
        load_backlog: RowsLoader,
        load_watchlist: RowsLoader,
        load_tracking: RowsLoader,
        load_library: LibraryLoader,
        load_achievements: AchievementLoader,
    ) -> None:
        self._load_backlog = load_backlog
        self._load_watchlist = load_watchlist
        self._load_tracking = load_tracking
        self._load_library = load_library
        self._load_achievements = load_achievements

    async def build(self, user: UserContext, achievement_scan_limit: int = 0) -> dict[str, Any]:
        (library_result, explicit, watch_rows, tracking_rows) = await asyncio.gather(
            self._load_library(user.steam_id),
            self._load_backlog(user.user_id),
            self._load_watchlist(user.user_id),
            self._load_tracking(user.user_id),
        )
        games, total, fetched_at = library_result
        explicit_by_id = {int(row["appid"]): row for row in explicit if row.get("appid") is not None}
        watched = [int(row["appid"]) for row in watch_rows if row.get("appid") is not None]
        tracked = [int(row["appid"]) for row in tracking_rows if row.get("appid") is not None]
        now = datetime.now(UTC)

        def days_since_play(game: GameRecord) -> int | None:
            if not game.last_played_at:
                return None
            try:
                moment = datetime.fromisoformat(game.last_played_at.replace("Z", "+00:00"))
                return max(0, (now - moment).days)
            except (ValueError, TypeError):
                return None

        never_started = [game for game in games if not (game.playtime_forever_minutes or 0)]
        started_not_recent = [
            game for game in games
            if (game.playtime_forever_minutes or 0) > 0
            and (age := days_since_play(game)) is not None and age > 30
        ]
        recently_active = [
            game for game in games if (age := days_since_play(game)) is not None and age <= 30
        ]
        terminal_states = {"finished", "dropped", "completed_100"}
        high_playtime_unknown = [
            game for game in games
            if (game.playtime_forever_minutes or 0) >= 1200
            and explicit_by_id.get(game.appid, {}).get("state") not in terminal_states
        ]
        recently_active.sort(key=lambda game: game.last_played_at or "", reverse=True)
        high_playtime_unknown.sort(key=lambda game: game.playtime_forever_minutes or 0, reverse=True)
        scan_candidates = high_playtime_unknown[:achievement_scan_limit]
        semaphore = asyncio.Semaphore(3)

        async def scan(game: GameRecord) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    result = await self._load_achievements(user.steam_id, game.appid, user.preferred_language)
                except Exception:
                    return None
            definitions = result.get("definitions", [])
            unlocked = {
                item.get("apiname") for item in result.get("player_achievements", [])
                if isinstance(item, dict) and item.get("achieved") and item.get("apiname")
            }
            named = [item for item in definitions if isinstance(item, dict) and item.get("name")]
            if not named:
                return None
            count = sum(item["name"] in unlocked for item in named)
            percent = round(count * 100 / len(named), 1)
            if 80 <= percent < 100:
                return {"game": game.model_dump(mode="json"), "unlocked": count,
                        "total": len(named), "completion_percent": percent}
            return None

        scanned = await asyncio.gather(*(scan(game) for game in scan_candidates))
        warnings = [
            "Inferred lists are behavioral suggestions, not user-declared completion or backlog state.",
            "Steam's visible library may omit never-launched free-to-play titles.",
        ]
        if achievement_scan_limit and len(scan_candidates) < len(high_playtime_unknown):
            warnings.append("Near-100% achievement view is truncated by achievement_scan_limit.")
        return {
            "explicit": explicit,
            "watchlist": watched,
            "price_tracking": tracked,
            "inferred": {
                "never_started": [game.model_dump(mode="json") for game in never_started[:50]],
                "started_not_recent": [game.model_dump(mode="json") for game in started_not_recent[:50]],
                "recently_active": [game.model_dump(mode="json") for game in recently_active[:50]],
                "high_playtime_unfinished_unknown": [
                    game.model_dump(mode="json") for game in high_playtime_unknown[:50]
                ],
                "near_100_percent": [item for item in scanned if item is not None],
                "classification_basis": {
                    "recently_active_max_days": 30,
                    "started_not_recent_min_days": 31,
                    "high_playtime_min_minutes": 1200,
                    "terminal_explicit_states_excluded_from_high_playtime": sorted(terminal_states),
                    "near_100_percent_min_completion_percent": 80,
                    "near_100_percent_max_completion_percent_exclusive": 100,
                    "near_100_percent_scan_limit": achievement_scan_limit,
                },
            },
            "provenance": {
                "source": "Steam Web API owned games plus local explicit backlog and watchlist",
                "provider": "steam_web_api+steam_companion_database",
                "fetched_at": fetched_at,
                "complete": True,
                "truncated": any(len(group) > 50 for group in
                                  (never_started, started_not_recent, recently_active, high_playtime_unknown)),
                "stale": False,
                "warnings": warnings,
            },
            "_summary": (len(explicit), len(watched), len(tracked), total),
        }
