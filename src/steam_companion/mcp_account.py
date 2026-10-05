from __future__ import annotations

from typing import Annotated, Any

from mcp.types import CallToolResult

from steam_companion.mcp_registration import RegistrationContext
from steam_companion.models import SteamAccount


def register_account_tools(ctx: RegistrationContext) -> None:
    @ctx.mcp.tool(name="get_steam_account", annotations=ctx.public)
    @ctx.strict_tool
    async def get_steam_account() -> Annotated[CallToolResult, SteamAccount]:
        response = await ctx.services.account.account(await ctx.user_context())
        return ctx.output(response, f"Connected Steam account: {response.persona_name or response.steam_id}.")

    if ctx.full_mode:
        @ctx.mcp.tool(name="get_player_summary", annotations=ctx.private)
        @ctx.strict_tool
        async def get_player_summary() -> Annotated[CallToolResult, dict[str, Any]]:
            result = await ctx.services.account.player_summary(await ctx.steam_id())
            return ctx.output(result, "Returned the authenticated Steam profile summary.")
