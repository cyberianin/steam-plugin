from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations

from steam_companion.app_services import AppServices
from steam_companion.wallet import WalletProvider


@dataclass(frozen=True, slots=True)
class RegistrationContext:
    mcp: MCPServer
    services: AppServices
    wallet: WalletProvider
    user: Callable[[], str]
    user_context: Callable[[], Awaitable[Any]]
    steam_id: Callable[[], Awaitable[str]]
    wallet_settings: Callable[[], Awaitable[dict[str, object]]]
    output: Callable[[Any, str], CallToolResult]
    strict_tool: Callable[[Any], Any]
    private: ToolAnnotations
    public: ToolAnnotations
    full_mode: bool
