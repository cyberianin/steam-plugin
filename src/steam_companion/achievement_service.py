from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from steam_companion.models import AchievementItem, AchievementSummary
from steam_companion.storage import Database
from steam_companion.steam import utc_now_iso

AchievementLoader = Callable[[str, int, str], Awaitable[dict[str, Any]]]


class AchievementService:
    """Normalizes player/schema data and explicitly records this successful observation."""

    def __init__(self, db: Database, load: AchievementLoader):
        self._db = db
        self._load = load

    async def observe_summary(self, user_id: str, steam_id: str, appid: int, language: str) -> AchievementSummary:
        result = await self._load(steam_id, appid, language)
        unlocked_by_name = {
            str(item.get("apiname")): item for item in result.get("player_achievements", [])
            if isinstance(item, dict) and item.get("apiname")
        }
        items: list[AchievementItem] = []
        for definition in result.get("definitions", []):
            if not isinstance(definition, dict) or not isinstance(definition.get("name"), str):
                continue
            player_item = unlocked_by_name.get(definition["name"], {})
            unlocked = bool(player_item.get("achieved"))
            hidden = bool(definition.get("hidden"))
            unlocked_at = player_item.get("unlocktime")
            items.append(AchievementItem(
                api_name=definition["name"],
                display_name=definition.get("displayName") if isinstance(definition.get("displayName"), str) else None,
                description=(definition.get("description") if isinstance(definition.get("description"), str) else None)
                if (unlocked or not hidden) else "Hidden achievement",
                unlocked=unlocked,
                unlocked_at=(datetime.fromtimestamp(unlocked_at, UTC).isoformat().replace("+00:00", "Z")
                             if isinstance(unlocked_at, int) and unlocked_at > 0 else None),
            ))
        unlocked = [item for item in items if item.unlocked]
        unlocked.sort(key=lambda item: item.unlocked_at or "", reverse=True)
        observed_at = str(result.get("_service_fetched_at", utc_now_iso()))
        await self._db.record_achievement_observation(
            user_id, appid, [item.api_name for item in unlocked], observed_at
        )
        total = len(items)
        return AchievementSummary(
            appid=appid, game_name=result.get("game_name") if isinstance(result.get("game_name"), str) else None,
            total=total, unlocked=len(unlocked), locked=total - len(unlocked),
            completion_percent=round(len(unlocked) * 100 / total, 1) if total else None,
            recently_unlocked=unlocked[:10],
            provenance={"source": "Steam Web API ISteamUserStats schema and player achievements",
                        "provider": "steam_web_api", "fetched_at": observed_at, "complete": True,
                        "truncated": len(unlocked) > 10, "stale": False,
                        "warnings": ["Locked hidden achievement descriptions are withheld to avoid spoilers."]},
        )
