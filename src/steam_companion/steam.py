from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from steam_companion.errors import ServiceError
from steam_companion.http_policy import steam_http_policy
from steam_companion.observability import emit


class SteamClient:
    """Domain-level Steam Web API adapter with bounded TTL cache and request coalescing."""

    API_ORIGIN = "https://api.steampowered.com"
    CACHE_MAX_ENTRIES = 128

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        library_ttl: int = 300,
        timeout: float = 9.0,
        connect_timeout: float = 3.0,
    ) -> None:
        self.http = http
        self.api_key = api_key
        self.library_ttl = library_ttl
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    async def _cached(
        self, key: str, ttl: int, loader: Callable[[], Awaitable[Any]], *, fresh: bool = False
    ) -> Any:
        async with self._lock:
            task = self._inflight.get(key)
            if task is not None:
                pass
            elif not fresh:
                hit = self._cache.get(key)
                if hit and hit[0] > time.monotonic():
                    self._cache.move_to_end(key)
                    emit(20, route="steam_cache", cache_hit=True, upstream="steam_web_api", status_class="2xx")
                    return hit[1]
            if task is None:
                task = asyncio.create_task(loader())
                self._inflight[key] = task
                task.add_done_callback(
                    lambda completed: asyncio.create_task(self._finish_cached(key, ttl, completed))
                )
        try:
            value = await asyncio.shield(task)
        except BaseException:
            # A disconnected caller leaves the shared loader shielded; finalize it
            # when it completes so unique cancelled requests cannot grow _inflight.
            if task.done():
                await self._finish_cached(key, ttl, task)
            raise
        await self._finish_cached(key, ttl, task)
        return value

    async def _finish_cached(self, key: str, ttl: int, task: asyncio.Task[Any]) -> None:
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
                try:
                    value = task.result()
                except BaseException:
                    return
                if isinstance(value, dict):
                    value.setdefault("_service_fetched_at", utc_now_iso())
                self._cache[key] = (time.monotonic() + ttl, value)
                self._cache.move_to_end(key)
                while len(self._cache) > self.CACHE_MAX_ENTRIES:
                    self._cache.popitem(last=False)

    async def _get(
        self,
        path: str,
        params: dict[str, Any],
        *,
        auth_error_code: str = "steam_api_auth_failed",
        auth_error_message: str = "The Steam Web API key was rejected.",
        auth_error_status: int = 502,
    ) -> dict[str, Any]:
        url = f"{self.API_ORIGIN}/{path.lstrip('/')}"
        host = steam_http_policy.validate_url(url)
        request_params = {"key": self.api_key, "format": "json", **params}
        emit(10, route="steam_web_api", cache_hit=False, upstream="api.steampowered.com", status_class="pending")
        response: httpx.Response | None = None
        for attempt in range(3):
            await steam_http_policy.acquire(host)
            try:
                response = await self.http.get(
                    url,
                    params=request_params,
                    timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
                    follow_redirects=False,
                )
            except httpx.TimeoutException as exc:
                if attempt == 2:
                    raise ServiceError("steam_timeout", "Steam did not respond before the timeout.", 504) from None
                await asyncio.sleep(min(2.0, 0.2 * (2**attempt)))
                continue
            except httpx.RequestError as exc:
                raise ServiceError("steam_unavailable", "Steam is temporarily unavailable.", 503) from None
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(steam_http_policy.retry_delay(response, attempt))
                continue
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(steam_http_policy.retry_delay(response, attempt))
                continue
            break
        assert response is not None
        if response.status_code in (401, 403):
            emit(30, route="steam_web_api", cache_hit=False, upstream="api.steampowered.com", status_class="4xx", normalized_error=auth_error_code)
            raise ServiceError(auth_error_code, auth_error_message, auth_error_status)
        if response.status_code == 429:
            emit(30, route="steam_web_api", cache_hit=False, upstream="api.steampowered.com", status_class="4xx", normalized_error="upstream_rate_limited")
            raise ServiceError("upstream_rate_limited", "Steam rate limited the request.", 503)
        if response.status_code >= 500:
            emit(30, route="steam_web_api", cache_hit=False, upstream="api.steampowered.com", status_class="5xx", normalized_error="steam_unavailable")
            raise ServiceError("steam_unavailable", "Steam is temporarily unavailable.", 503)
        if response.status_code != 200:
            raise ServiceError("steam_unavailable", "Steam returned an unexpected response.", 502)
        try:
            body = response.json()
        except ValueError as exc:
            raise ServiceError("steam_unavailable", "Steam returned malformed JSON.", 502) from exc
        if not isinstance(body, dict):
            raise ServiceError("steam_unavailable", "Steam returned malformed data.", 502)
        return body

    async def get_owned_games(self, steam_id: str, force: bool = False) -> dict[str, Any]:
        key = f"owned:{steam_id}"
        return await self._cached(
            key,
            self.library_ttl,
            lambda: self._load_owned_games(steam_id),
            fresh=force,
        )

    async def _load_owned_games(self, steam_id: str) -> dict[str, Any]:
        body = await self._get(
            "IPlayerService/GetOwnedGames/v1/",
            {
                "steamid": steam_id,
                "include_appinfo": "true",
                "include_played_free_games": "true",
            },
        )
        response = body.get("response")
        if not isinstance(response, dict) or "game_count" not in response or not isinstance(response.get("games"), list):
            raise ServiceError(
                "library_private",
                "Steam did not expose this account's game details. Set Game details to public and try again.",
                403,
            )
        if not isinstance(response.get("game_count"), int) or response["game_count"] != len(response["games"]):
            raise ServiceError("library_incomplete", "Steam returned a partial owned-games collection.", 502)
        return response

    async def get_recent_games(self, steam_id: str, count: int = 0) -> dict[str, Any]:
        key = f"recent:{steam_id}:{count}"
        return await self._cached(
            key,
            min(self.library_ttl, 180),
            lambda: self._load_recent_games(steam_id, count),
        )

    async def get_current_players(self, appid: int) -> dict[str, Any]:
        if appid <= 0:
            raise ServiceError("invalid_appid", "AppID must be positive.", 400)

        async def load() -> dict[str, Any]:
            body = await self._get("ISteamUserStats/GetNumberOfCurrentPlayers/v1/", {"appid": appid})
            response = body.get("response")
            count = response.get("player_count") if isinstance(response, dict) else None
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ServiceError("current_players_unavailable", "Steam returned no current player count.", 502)
            return {"player_count": count}

        return await self._cached(f"current_players:{appid}", 60, load)

    async def _load_recent_games(self, steam_id: str, count: int) -> dict[str, Any]:
        body = await self._get(
            "IPlayerService/GetRecentlyPlayedGames/v1/",
            {"steamid": steam_id, "count": count},
        )
        response = body.get("response")
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("total_count"), int)
            or not isinstance(response.get("games"), list)
        ):
            raise ServiceError("steam_unavailable", "Steam returned incomplete recent-games data.", 502)
        if response["total_count"] != len(response["games"]):
            raise ServiceError("recent_games_incomplete", "Steam returned a partial recent-games collection.", 502)
        return response

    async def get_player_summary(self, steam_id: str) -> dict[str, Any]:
        key = f"player:{steam_id}"
        return await self._cached(key, self.library_ttl, lambda: self._load_player_summary(steam_id))

    async def get_steam_level(self, steam_id: str) -> int | None:
        key = f"steam_level:{steam_id}"
        result = await self._cached(key, max(self.library_ttl, 3600), lambda: self._load_steam_level(steam_id))
        level = result.get("level")
        return level if isinstance(level, int) else None

    async def _load_steam_level(self, steam_id: str) -> dict[str, Any]:
        body = await self._get("IPlayerService/GetSteamLevel/v1/", {"steamid": steam_id})
        response = body.get("response")
        level = response.get("player_level") if isinstance(response, dict) else None
        return {"level": level if isinstance(level, int) and not isinstance(level, bool) else None}

    async def get_player_summaries(self, steam_ids: list[str]) -> list[dict[str, Any]]:
        if not steam_ids or len(steam_ids) > 100:
            raise ServiceError("invalid_friend_batch", "Steam profile batches must contain 1 to 100 IDs.", 400)
        normalized_ids = list(dict.fromkeys(steam_ids))
        key = f"players:{','.join(normalized_ids)}"
        return await self._cached(key, self.library_ttl, lambda: self._load_player_summaries(normalized_ids))

    async def _load_player_summaries(self, steam_ids: list[str]) -> list[dict[str, Any]]:
        body = await self._get("ISteamUser/GetPlayerSummaries/v2/", {"steamids": ",".join(steam_ids)})
        players = body.get("response", {}).get("players")
        if not isinstance(players, list):
            raise ServiceError("steam_unavailable", "Steam returned incomplete player data.", 502)
        return [player for player in players if isinstance(player, dict)]

    async def get_friend_list(self, steam_id: str) -> list[dict[str, Any]]:
        key = f"friends:{steam_id}"
        return await self._cached(key, min(self.library_ttl, 300), lambda: self._load_friend_list(steam_id))

    async def _load_friend_list(self, steam_id: str) -> list[dict[str, Any]]:
        body = await self._get(
            "ISteamUser/GetFriendList/v1/",
            {"steamid": steam_id, "relationship": "friend"},
            auth_error_code="friend_list_private",
            auth_error_message="Steam did not expose this account's friend list.",
            auth_error_status=403,
        )
        friends_list = body.get("friendslist")
        friends = friends_list.get("friends") if isinstance(friends_list, dict) else None
        if not isinstance(friends, list):
            raise ServiceError("friend_list_unavailable", "Steam returned incomplete friend-list data.", 502)
        return [
            {"steamid": str(item["steamid"]), "friend_since": item.get("friend_since")}
            for item in friends
            if isinstance(item, dict)
            and isinstance(item.get("steamid"), str)
            and len(item["steamid"]) == 17
            and item["steamid"].isdigit()
        ]

    async def get_game_achievements(self, steam_id: str, appid: int, language: str = "english") -> dict[str, Any]:
        key = f"achievements:{steam_id}:{appid}:{language}"
        return await self._cached(key, min(self.library_ttl, 900), lambda: self._load_game_achievements(steam_id, appid, language))

    async def get_wishlist(self, steam_id: str, force: bool = False) -> dict[str, Any]:
        key = f"wishlist:{steam_id}"
        return await self._cached(
            key, min(self.library_ttl, 300), lambda: self._load_wishlist(steam_id), fresh=force
        )

    async def _load_wishlist(self, steam_id: str) -> dict[str, Any]:
        body = await self._get("IWishlistService/GetWishlist/v1/", {"steamid": steam_id})
        response = body.get("response")
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise ServiceError("wishlist_unavailable", "Steam did not expose this account's wishlist.", 403)
        normalized: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("appid"), int):
                raise ServiceError("wishlist_incomplete", "Steam returned incomplete wishlist data.", 502)
            appid = item["appid"]
            if appid <= 0 or appid in seen:
                continue
            seen.add(appid)
            date_added = item.get("date_added")
            normalized.append({
                "appid": appid,
                "priority": item.get("priority") if isinstance(item.get("priority"), int) else None,
                "date_added": date_added if isinstance(date_added, int) and date_added > 0 else None,
            })
        return {"items": normalized, "total": len(normalized)}

    async def _load_game_achievements(self, steam_id: str, appid: int, language: str) -> dict[str, Any]:
        schema_task = self._get(
            "ISteamUserStats/GetSchemaForGame/v2/",
            {"appid": appid, "l": language},
        )
        player_task = self._get(
            "ISteamUserStats/GetPlayerAchievements/v1/",
            {"steamid": steam_id, "appid": appid, "l": language},
        )
        schema_body, player_body = await asyncio.gather(schema_task, player_task)
        schema_game = schema_body.get("game")
        schema = schema_game.get("availableGameStats") if isinstance(schema_game, dict) else None
        definitions = schema.get("achievements") if isinstance(schema, dict) else None
        player = player_body.get("playerstats")
        if not isinstance(definitions, list) or not isinstance(player, dict):
            raise ServiceError("achievements_unavailable", "Steam did not provide achievement data for this game.", 404)
        if player.get("success") is not True or not isinstance(player.get("achievements"), list):
            raise ServiceError("achievements_unavailable", "Steam did not expose achievements for this account and game.", 403)
        return {
            "game_name": schema_game.get("gameName") if isinstance(schema_game.get("gameName"), str) else player.get("gameName"),
            "definitions": definitions,
            "player_achievements": player["achievements"],
        }

    async def _load_player_summary(self, steam_id: str) -> dict[str, Any]:
        body = await self._get("ISteamUser/GetPlayerSummaries/v2/", {"steamids": steam_id})
        players = body.get("response", {}).get("players")
        if not isinstance(players, list):
            raise ServiceError("steam_unavailable", "Steam returned incomplete player data.", 502)
        return players[0] if players else {}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
