from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from steam_companion.app_services import AppServices
from steam_companion.models import CoopContext, GameComparison, GameContext, PackageAnalysis


def register_composite_tools(
    mcp: MCPServer,
    services: AppServices,
    user_context: Callable[[], Awaitable[Any]],
    steam_id: Callable[[], Awaitable[str]],
    output: Callable[[Any, str], CallToolResult],
    strict_tool: Callable[[Any], Any],
    private: ToolAnnotations,
    public: ToolAnnotations,
    *, full_mode: bool,
) -> None:
    @mcp.tool(name="get_backlog", annotations=private)
    @strict_tool
    async def get_backlog(
        achievement_scan_limit: Annotated[int, Field(strict=True, ge=0, le=10)] = 0,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        result = await services.backlog.build(await user_context(), achievement_scan_limit)
        explicit_count, watched_count, tracked_count, total = result.pop("_summary")
        return output(
            result,
            f"Backlog has {explicit_count} explicit states, {watched_count} local watchlist entries, "
            f"and {tracked_count} tracked prices across {total} visible games.",
        )

    @mcp.tool(name="get_coop_context", annotations=public)
    @strict_tool
    async def get_coop_context(
        appids: Annotated[list[Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]], Field(min_length=1, max_length=20)],
        friend_limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 10,
    ) -> Annotated[CallToolResult, CoopContext]:
        result = await services.coop.build(await steam_id(), appids, friend_limit)
        return output(result, f"Found {len(result.matches)} friends sharing one or more candidate games.")

    @mcp.tool(name="game_context", annotations=public)
    @strict_tool
    async def game_context(
        appid: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)],
        fresh: bool = False,
    ) -> Annotated[CallToolResult, GameContext]:
        result = await services.game_context.build(appid, await user_context(), fresh=fresh)
        return output(result, f"Built partial-tolerant context for AppID {appid}.")

    if full_mode:
        @mcp.tool(name="analyze_package", annotations=public)
        @strict_tool
        async def analyze_package(
            package_id: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)],
        ) -> Annotated[CallToolResult, PackageAnalysis]:
            analysis = await services.package_analysis.analyze(package_id, await user_context())
            return output(
                analysis,
                f"Package {package_id} contains {len(analysis.apps)} visible games; "
                f"{analysis.duplicate_owned_count} are already owned.",
            )

        @mcp.tool(name="compare_games", annotations=public)
        @strict_tool
        async def compare_games(
            appids: Annotated[list[Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]], Field(min_length=2, max_length=5)],
        ) -> Annotated[CallToolResult, GameComparison]:
            result = await services.comparison.compare(appids, await user_context())
            return output(result, f"Compared {result.returned} Steam games.")
