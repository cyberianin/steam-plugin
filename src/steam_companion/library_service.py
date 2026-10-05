from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from statistics import median

from steam_companion.library import LibrarySnapshot
from steam_companion.models import GameRecord, LibraryAnalysis, LibraryPage
from steam_companion.models import (
    ActivityDelta, ActivityHistory, EventHistory, LibraryHistory, LibraryHistoryPoint,
    LibrarySummary, ObservedEvent, OwnershipResult, RecentGames,
)
from steam_companion.pagination import SnapshotCursor
from steam_companion.storage import Database
from steam_companion.steam import utc_now_iso

SnapshotLoader = Callable[[str, bool], Awaitable[tuple[LibrarySnapshot, int, str]]]


class LibraryService:
    def __init__(
        self, load_snapshot: SnapshotLoader, observe_snapshot: SnapshotLoader,
        cursor_secret: bytes, db: Database,
        load_recent: Callable[..., Awaitable[dict]],
    ):
        self._load_snapshot = load_snapshot
        self._observe_snapshot = observe_snapshot
        self._cursor_secret = cursor_secret
        self._db = db
        self._load_recent = load_recent

    async def _snapshot(self, steam_id: str, fresh: bool, observe: bool) -> tuple[LibrarySnapshot, int, str]:
        loader = self._observe_snapshot if observe else self._load_snapshot
        return await loader(steam_id, fresh)

    async def summary(self, steam_id: str, *, observe: bool = False) -> LibrarySummary:
        games, total, fetched_at = await self._snapshot(steam_id, False, observe)
        playtimes = [game.playtime_forever_minutes or 0 for game in games]
        played = sum(value > 0 for value in playtimes)
        top = sorted(games, key=lambda item: item.playtime_forever_minutes or 0, reverse=True)[:10]
        return LibrarySummary(
            total_games=total, played_games=played, unplayed_games=max(0, total - played),
            total_playtime_minutes=sum(playtimes), total_playtime_hours=round(sum(playtimes) / 60, 2),
            recently_played_games=None, top_played=top,
            provenance={"source": "Steam Web API IPlayerService.GetOwnedGames", "provider": "steam_web_api",
                        "fetched_at": fetched_at, "complete": len(games) == total, "truncated": False,
                        "stale": False, "warnings": ["Steam omits never-launched free-to-play titles from this endpoint."]},
        )

    async def get_game(self, steam_id: str, appid: int, *, observe: bool = False) -> GameRecord | OwnershipResult:
        games, _, _ = await self._snapshot(steam_id, False, observe)
        match = games.by_appid.get(appid)
        if match:
            return match
        return OwnershipResult(appid=appid, ownership="unknown", owned=None,
                               reason="not_in_response_may_be_unplayed_free_to_play")

    async def check_games(
        self, steam_id: str, appids: list[int], *, observe: bool = False
    ) -> list[OwnershipResult]:
        games, _, _ = await self._snapshot(steam_id, False, observe)
        by_appid = games.by_appid
        return [OwnershipResult(
            appid=appid, ownership="owned" if appid in by_appid else "unknown",
            owned=True if appid in by_appid else None,
            playtime_minutes=by_appid[appid].playtime_minutes if appid in by_appid else None,
            last_played_at=by_appid[appid].last_played_at if appid in by_appid else None,
            name=by_appid[appid].name if appid in by_appid else None,
            reason="present_in_owned_games" if appid in by_appid else "not_in_response_entitlement_unknown",
        ) for appid in appids]

    async def recent(self, steam_id: str, limit: int) -> RecentGames:
        from steam_companion.library import game_from_api

        result = await self._load_recent(steam_id, count=0)
        games = [game_from_api(item, recent=True) for item in result.get("games", [])]
        games.sort(key=lambda item: item.last_played_at or "", reverse=True)
        return RecentGames(
            games=games[:limit], returned=min(limit, len(games)),
            provenance={"source": "Steam Web API IPlayerService.GetRecentlyPlayedGames", "provider": "steam_web_api",
                        "fetched_at": str(result.get("_service_fetched_at", utc_now_iso())),
                        "complete": True, "truncated": len(games) > limit, "stale": False, "warnings": []},
        )

    async def history(self, user_id: str, limit: int) -> LibraryHistory:
        rows = await self._db.get_library_snapshots(user_id, limit)
        points = [LibraryHistoryPoint.model_validate(row) for row in reversed(rows)]
        return LibraryHistory(
            points=points, returned=len(points), source="request-driven daily Steam library snapshots",
            provenance={"source": "local SQLite library_snapshots", "provider": "steam_companion_database",
                        "fetched_at": utc_now_iso(), "complete": True, "truncated": len(rows) >= limit,
                        "stale": False,
                        "warnings": ["Snapshots are captured on observed library reads; no background polling is enabled."]},
        )

    async def activity(self, user_id: str, limit: int) -> ActivityHistory:
        rows = await self._db.get_activity_deltas(user_id, limit)
        points = [ActivityDelta.model_validate(row) for row in reversed(rows)]
        return ActivityHistory(
            points=points, returned=len(points),
            provenance={"source": "request-driven changes between complete Steam library observations",
                        "provider": "steam_companion_database", "fetched_at": utc_now_iso(), "complete": True,
                        "truncated": len(rows) >= limit, "stale": False,
                        "warnings": ["History starts with the first observed baseline; changes before that are unknown.",
                                     "Only positive playtime deltas confirmed by a later complete Steam response are recorded."]},
        )

    async def events(self, user_id: str, limit: int) -> EventHistory:
        rows = await self._db.get_event_journal(user_id, limit)
        events = [ObservedEvent.model_validate(row) for row in reversed(rows)]
        return EventHistory(
            events=events, returned=len(events),
            provenance={"source": "request-driven changes between complete Steam library observations",
                        "provider": "steam_companion_database", "fetched_at": utc_now_iso(), "complete": True,
                        "truncated": len(rows) >= limit, "stale": False,
                        "warnings": ["Event history starts after the first observed baseline; earlier changes are unknown.",
                                     "Confirmed acquisitions, first plays, positive playtime changes, wishlist transitions, achievement unlocks, and price transitions are recorded."]},
        )

    async def analyze(self, steam_id: str, *, observe: bool = False) -> LibraryAnalysis:
        games, total, fetched_at = await self._snapshot(steam_id, False, observe)
        now = datetime.now(UTC)
        played = [game for game in games if (game.playtime_forever_minutes or 0) > 0]
        playtimes = sorted(game.playtime_forever_minutes or 0 for game in games)

        def percentile(fraction: float) -> float | None:
            if not playtimes:
                return None
            position = (len(playtimes) - 1) * fraction
            low = int(position)
            high = min(low + 1, len(playtimes) - 1)
            return playtimes[low] + (playtimes[high] - playtimes[low]) * (position - low)

        def age_days(game: GameRecord) -> int | None:
            if not game.last_played_at:
                return None
            try:
                moment = datetime.fromisoformat(game.last_played_at.replace("Z", "+00:00"))
                return max(0, (now - moment).days)
            except ValueError:
                return None

        recent = [game for game in games if (age := age_days(game)) is not None and age <= 30]
        dormant = [game for game in played if (age := age_days(game)) is not None and age > 180]
        abandoned = [game for game in played if (age := age_days(game)) is not None and age > 365]
        recent.sort(key=lambda game: game.last_played_at or "", reverse=True)
        dormant.sort(key=lambda game: age_days(game) or 0, reverse=True)
        abandoned.sort(key=lambda game: age_days(game) or 0, reverse=True)
        return LibraryAnalysis(
            total_games=total, played_games=len(played), unplayed_games=max(0, total - len(played)),
            total_playtime_minutes=sum(playtimes), median_playtime_minutes=median(playtimes) if playtimes else None,
            playtime_p25_minutes=percentile(0.25), playtime_p75_minutes=percentile(0.75),
            recently_active=recent[:10], dormant=dormant[:20], abandoned=abandoned[:20],
            classification_basis={"recently_active_max_days": 30, "dormant_min_days_since_last_play": 181,
                                  "abandoned_min_days_since_last_play": 366,
                                  "dormant_and_abandoned_require_nonzero_playtime": True,
                                  "abandoned_is_behavioral_label_not_user_assertion": True},
            provenance={"source": "Steam Web API IPlayerService.GetOwnedGames", "provider": "steam_web_api",
                        "fetched_at": fetched_at, "complete": True,
                        "truncated": len(recent) > 10 or len(dormant) > 20 or len(abandoned) > 20,
                        "stale": False, "warnings": ["Never-launched free-to-play titles may be absent from this endpoint."]},
        )

    async def page(
        self, user_id: str, steam_id: str, *, query: str | None, played: str, sort: str,
        limit: int, cursor: str | None, fresh: bool = False, observe: bool = False,
    ) -> LibraryPage:
        normalized_query = (query or "").strip().casefold()
        filters = {"query": normalized_query, "played": played, "sort": sort}
        principal = hashlib.sha256(user_id.encode()).hexdigest()[:24]
        games, upstream_total, fetched_at = await self._snapshot(steam_id, fresh, observe)
        offset = SnapshotCursor.decode(cursor, principal, filters, self._cursor_secret, fetched_at, "library") if cursor else 0
        filtered = [game for game in games
                    if (not normalized_query or normalized_query in (game.name or "").casefold())
                    and (played == "any" or (played == "played") == ((game.playtime_minutes or 0) > 0))]
        if sort == "name":
            filtered.sort(key=lambda game: ((game.name or "").casefold(), game.appid))
        elif sort == "playtime_desc":
            filtered.sort(key=lambda game: (-(game.playtime_minutes or 0), (game.name or "").casefold(), game.appid))
        elif sort == "playtime_asc":
            filtered.sort(key=lambda game: ((game.playtime_minutes or 0), (game.name or "").casefold(), game.appid))
        else:
            filtered.sort(key=lambda game: (game.last_played_at is not None, game.last_played_at or "", game.appid), reverse=True)
        page = filtered[offset:offset + limit]
        next_offset = offset + len(page)
        next_cursor = (SnapshotCursor.encode(next_offset, principal, filters, self._cursor_secret, fetched_at, "library")
                       if next_offset < len(filtered) else None)
        return LibraryPage(
            total_matching=len(filtered), returned=len(page), complete=len(games) == upstream_total,
            next_cursor=next_cursor, data_fetched_at=fetched_at, games=page,
            provenance={"source": "Steam Web API owned-games snapshot", "provider": "steam_web_api",
                        "fetched_at": fetched_at, "complete": len(games) == upstream_total,
                        "truncated": next_cursor is not None, "stale": False,
                        "warnings": ["Cursor is bound to this library snapshot; refreshes can make it stale."]},
        )
