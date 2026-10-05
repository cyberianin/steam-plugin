from __future__ import annotations

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from steam_companion.mcp_registration import RegistrationContext
from steam_companion.models import (
    AchievementSummary, ActivityHistory, EventHistory, GameRecord, LibraryAnalysis,
    LibraryHistory, LibraryPage, LibrarySummary, OwnershipResult, RecentGames,
)


def register_library_tools(ctx: RegistrationContext) -> None:
    mcp, services, output, strict = ctx.mcp, ctx.services, ctx.output, ctx.strict_tool
    private = ctx.private

    @mcp.tool(name="get_library_summary", annotations=private)
    @strict
    async def get_library_summary() -> Annotated[CallToolResult, LibrarySummary]:
        result = await services.library.summary(await ctx.steam_id(), observe=True)
        return output(result, f"Visible Steam library: {result.total_games} games; {result.played_games} played.")

    @mcp.tool(name="analyze_library", annotations=private)
    @strict
    async def analyze_library() -> Annotated[CallToolResult, LibraryAnalysis]:
        result = await services.library.analyze(await ctx.steam_id(), observe=True)
        return output(result, f"Analyzed {result.total_games} visible games; {result.played_games} have recorded playtime.")

    @mcp.tool(name="get_library_history", annotations=private)
    @strict
    async def get_library_history(limit: Annotated[int, Field(strict=True, ge=1, le=365)] = 90) -> Annotated[CallToolResult, LibraryHistory]:
        result = await services.library.history(ctx.user(), limit)
        return output(result, f"Returned {result.returned} saved library history points.")

    if ctx.full_mode:
        @mcp.tool(name="get_activity_history", annotations=private)
        @strict
        async def get_activity_history(limit: Annotated[int, Field(strict=True, ge=1, le=365)] = 90) -> Annotated[CallToolResult, ActivityHistory]:
            result = await services.library.activity(ctx.user(), limit)
            return output(result, f"Returned {result.returned} confirmed playtime changes.")

        @mcp.tool(name="get_event_history", annotations=private)
        @strict
        async def get_event_history(limit: Annotated[int, Field(strict=True, ge=1, le=365)] = 90) -> Annotated[CallToolResult, EventHistory]:
            result = await services.library.events(ctx.user(), limit)
            return output(result, f"Returned {result.returned} confirmed library events.")

    @mcp.tool(name="list_library_games", annotations=private)
    @strict
    async def list_library_games(
        query: Annotated[str | None, Field(max_length=120)] = None,
        played: Annotated[str, Field(pattern="^(any|played|unplayed)$")] = "any",
        sort: Annotated[str, Field(pattern="^(name|playtime_desc|playtime_asc|last_played_desc)$")] = "name",
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Field(max_length=512)] = None,
        fresh: bool = False,
    ) -> Annotated[CallToolResult, LibraryPage]:
        result = await services.library.page(
            ctx.user(), await ctx.steam_id(), query=query, played=played, sort=sort,
            limit=limit, cursor=cursor, fresh=fresh, observe=True,
        )
        return output(result, f"Returned {result.returned} of {result.total_matching} matching games.")

    @mcp.tool(name="get_library_game", annotations=private)
    @strict
    async def get_library_game(appid: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]) -> Annotated[CallToolResult, GameRecord | OwnershipResult]:
        result = await services.library.get_game(await ctx.steam_id(), appid, observe=True)
        if isinstance(result, GameRecord):
            return output(result, f"{result.name or f'Steam AppID {appid}'} is in the visible library.")
        return output(result, "Steam did not return this AppID; free-to-play entitlement is not determined.")

    @mcp.tool(name="check_library_games", annotations=private)
    @strict
    async def check_library_games(
        appids: Annotated[list[Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]], Field(min_length=1, max_length=100)],
    ) -> Annotated[CallToolResult, list[OwnershipResult]]:
        result = await services.library.check_games(await ctx.steam_id(), appids, observe=True)
        return output(result, f"Checked {len(result)} games against the visible library.")

    @mcp.tool(name="get_recent_games", annotations=private)
    @strict
    async def get_recent_games(limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20) -> Annotated[CallToolResult, RecentGames]:
        result = await services.library.recent(await ctx.steam_id(), limit)
        return output(result, f"Returned {result.returned} recent games.")

    @mcp.tool(name="get_achievement_summary", annotations=private)
    @strict
    async def get_achievement_summary(
        appid: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)],
    ) -> Annotated[CallToolResult, AchievementSummary]:
        result = await services.achievements.observe_summary(
            ctx.user(), await ctx.steam_id(), appid, (await ctx.user_context()).preferred_language
        )
        return output(result, f"{result.unlocked} of {result.total} achievements unlocked for AppID {appid}.")
