from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from steam_companion.library import LibrarySnapshot
from steam_companion.models import CoopContext, FriendCoopMatch, FriendSharedGame

FriendsLoader = Callable[[str], Awaitable[list[dict[str, Any]]]]
LibraryLoader = Callable[[str], Awaitable[dict[str, Any]]]
SummariesLoader = Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
OwnerLibraryLoader = Callable[[str], Awaitable[tuple[LibrarySnapshot, int, str]]]


class CoopService:
    MAX_FRIENDS = 20
    LIBRARY_CONCURRENCY = 3

    def __init__(
        self,
        get_friends: FriendsLoader,
        get_library: LibraryLoader,
        get_summaries: SummariesLoader,
        load_owner_library: OwnerLibraryLoader,
    ) -> None:
        self._get_friends = get_friends
        self._get_library = get_library
        self._get_summaries = get_summaries
        self._load_owner_library = load_owner_library

    async def build(self, steam_id: str, appids: list[int], friend_limit: int = 10) -> CoopContext:
        limit = min(max(friend_limit, 1), self.MAX_FRIENDS)
        friends, owner_result = await asyncio.gather(
            self._get_friends(steam_id), self._load_owner_library(steam_id)
        )
        owner_library, _, fetched_at = owner_result
        owned_by_appid = owner_library.by_appid
        candidates = list(dict.fromkeys(appid for appid in appids if appid in owned_by_appid))
        selected = sorted(
            friends, key=lambda item: item.get("friend_since")
            if isinstance(item.get("friend_since"), int) else 0, reverse=True
        )[:limit]
        warnings: list[str] = []
        if len(candidates) != len(set(appids)):
            warnings.append("Candidates not present in your visible library were excluded.")
        matches: list[FriendCoopMatch] = []
        public_count = 0
        inaccessible = 0
        if candidates:
            semaphore = asyncio.Semaphore(self.LIBRARY_CONCURRENCY)

            async def read_library(friend: dict[str, Any]) -> tuple[str, dict[str, Any] | BaseException]:
                friend_id = str(friend.get("steamid", ""))
                async with semaphore:
                    try:
                        return friend_id, await self._get_library(friend_id)
                    except Exception as exc:
                        return friend_id, exc

            results = await asyncio.gather(*(read_library(item) for item in selected))
            match_pairs: list[tuple[str, list[FriendSharedGame]]] = []
            for friend_id, response in results:
                if isinstance(response, BaseException):
                    inaccessible += 1
                    continue
                games = response.get("games")
                if not isinstance(games, list):
                    inaccessible += 1
                    continue
                public_count += 1
                friend_ids = {
                    item["appid"] for item in games if isinstance(item, dict)
                    and isinstance(item.get("appid"), int) and not isinstance(item.get("appid"), bool)
                }
                shared = [
                    FriendSharedGame(appid=appid, name=owned_by_appid[appid].name)
                    for appid in candidates if appid in friend_ids
                ]
                if shared:
                    match_pairs.append((friend_id, shared))
            if match_pairs:
                try:
                    profiles = await self._get_summaries([friend_id for friend_id, _ in match_pairs])
                    by_id = {str(row.get("steamid")): row for row in profiles if isinstance(row, dict)}
                    for friend_id, shared in match_pairs:
                        profile = by_id.get(friend_id, {})
                        matches.append(FriendCoopMatch(
                            persona_name=profile.get("personaname") if isinstance(profile.get("personaname"), str) else None,
                            profile_url=profile.get("profileurl") if isinstance(profile.get("profileurl"), str) else None,
                            shared_games=shared,
                        ))
                except Exception:
                    warnings.append("Friend profile names were unavailable; game overlap is still complete.")
                    matches = [FriendCoopMatch(persona_name=None, profile_url=None, shared_games=shared)
                               for _, shared in match_pairs]
        if inaccessible:
            warnings.append(f"{inaccessible} friend libraries were private, incomplete, or unavailable and were excluded.")
        if len(friends) > len(selected):
            warnings.append("Friend checking was limited; increase friend_limit to check more friends.")
        return CoopContext(
            friends_total=len(friends), friends_checked=len(selected) if candidates else 0,
            friends_with_public_libraries=public_count, matches=matches,
            provenance={
                "source": "Steam Web API friend list and visible owned-games libraries",
                "provider": "steam_web_api", "fetched_at": fetched_at,
                "complete": not inaccessible and len(friends) <= len(selected),
                "truncated": bool(inaccessible) or len(friends) > len(selected),
                "stale": False, "warnings": warnings,
            },
        )
