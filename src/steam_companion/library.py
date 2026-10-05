from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from steam_companion.models import GameRecord


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    """Immutable view over one fully fetched Steam library generation."""

    items: tuple[GameRecord, ...]
    by_appid: Mapping[int, GameRecord]
    fetched_at: str
    total: int

    @classmethod
    def build(cls, items: list[GameRecord], fetched_at: str, total: int) -> LibrarySnapshot:
        immutable_items = tuple(items)
        return cls(
            items=immutable_items,
            by_appid=MappingProxyType({game.appid: game for game in immutable_items}),
            fetched_at=fetched_at,
            total=total,
        )

    def __iter__(self) -> Iterator[GameRecord]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


def game_from_api(game: dict[str, Any], *, recent: bool = False) -> GameRecord:
    last_played = game.get("rtime_last_played")
    if isinstance(last_played, int) and last_played > 0:
        last_played_at = datetime.fromtimestamp(last_played, UTC).isoformat().replace("+00:00", "Z")
    else:
        last_played_at = None
    forever = game.get("playtime_forever")
    two_weeks = game.get("playtime_2weeks")
    playtime = int(forever) if isinstance(forever, int) and not isinstance(forever, bool) else None
    icon_hash = game.get("img_icon_url")
    icon_url = (
        f"https://media.steampowered.com/steamcommunity/public/images/apps/{int(game['appid'])}/{icon_hash}.jpg"
        if isinstance(icon_hash, str) and len(icon_hash) == 40
        and all(char in "0123456789abcdef" for char in icon_hash.lower())
        else None
    )
    platform_playtime = {
        platform: int(game[key])
        for platform, key in (
            ("windows", "playtime_windows_forever"),
            ("mac", "playtime_mac_forever"),
            ("linux", "playtime_linux_forever"),
        )
        if isinstance(game.get(key), int) and not isinstance(game.get(key), bool)
    }
    return GameRecord(
        appid=int(game["appid"]),
        name=game.get("name") if isinstance(game.get("name"), str) else None,
        playtime_forever_minutes=playtime,
        playtime_minutes=playtime,
        playtime_hours=round(playtime / 60, 2) if playtime is not None else None,
        playtime_2weeks_minutes=(
            int(two_weeks) if isinstance(two_weeks, int) and not isinstance(two_weeks, bool) else None
        ),
        last_played_at=last_played_at,
        icon_url=icon_url,
        platform_playtime_minutes=platform_playtime,
        ownership="owned",
        owned=True,
        source="steam_web_api",
    )
