from __future__ import annotations

import inspect
from functools import wraps
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
# Field is resolved from module globals by pydantic's dynamically created tool schemas.
from pydantic import ConfigDict, Field, create_model

from steam_companion.config import Settings
from steam_companion.errors import ServiceError
from steam_companion.models import Capabilities, GameMetadata, StoreOffer
from steam_companion.providers import SteamProvider
from steam_companion.oauth import OAuthService
from steam_companion.library import LibrarySnapshot, game_from_api
from steam_companion.observations import ObservationService
from steam_companion.app_services import AppServices
from steam_companion.account_service import AccountService
from steam_companion.review_service import ReviewService
from steam_companion.capabilities_service import CapabilitiesService
from steam_companion.mcp_composites import register_composite_tools
from steam_companion.mcp_registration import RegistrationContext
from steam_companion.mcp_library import register_library_tools
from steam_companion.mcp_store import register_store_tools
from steam_companion.mcp_account import register_account_tools
from steam_companion.mcp_prompts import register_prompts
from steam_companion.backlog import BacklogService
from steam_companion.comparison import GameComparisonService
from steam_companion.coop import CoopService
from steam_companion.game_context import GameContextService
from steam_companion.library_service import LibraryService
from steam_companion.achievement_service import AchievementService
from steam_companion.wishlist_service import WishlistService
from steam_companion.price_history import PriceHistoryService
from steam_companion.package_analysis import PackageAnalysisService
from steam_companion.purchase_context import PurchaseContextService
from steam_companion.request_context import (
    RequestContext,
    UserContext,
    WalletSnapshot,
    current_request_context,
)
from steam_companion.storage import Database
from steam_companion.steam import utc_now_iso
from steam_companion.store import StoreProvider
from steam_companion.wallet import LocalWalletProvider, WalletProvider
from steam_companion.storefront_service import StorefrontService


def create_mcp_server(
    settings: Settings,
    db: Database,
    auth: OAuthService,
    steam: SteamProvider,
    store: StoreProvider,
    wallet_provider: WalletProvider | None = None,
) -> MCPServer:
    mcp = MCPServer(
        name="Steam Companion",
        title="Steam Companion",
        description="Read-only personal Steam account context.",
        instructions=(
            "The owned-games response is the authoritative source for visible ownership and playtime. "
            "Store prices include source and freshness. Wallet data may be manual rather than live. "
            "Treat unavailable or unknown data as distinct from zero or false. Use composite tools for compact results."
        ),
        version="0.1.0",
    )
    private_annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    public_annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
    wallet = wallet_provider or LocalWalletProvider(settings)
    observations = ObservationService(db)

    def request_context() -> RequestContext:
        context = current_request_context.get()
        if context is None:
            raise ServiceError("unauthenticated", "Steam account authorization is required.", 401)
        return context

    def user() -> str:
        return request_context().principal.user_id

    async def user_settings() -> UserContext:
        context = request_context()
        if context.user_context is not None:
            return context.user_context
        loaded = await auth.get_user_settings(context.principal.user_id)
        user_context = UserContext(
            user_id=context.principal.user_id,
            steam_id=context.principal.steam_id,
            country_code=str(loaded["store_country_code"]),
            preferred_language=str(loaded["preferred_language"]),
            expected_currency=str(loaded["expected_currency"]),
        )
        context.local.wallet = WalletSnapshot(
            amount_minor=int(loaded["wallet_amount_minor"]) if loaded.get("wallet_amount_minor") is not None else None,
            currency=str(loaded["wallet_currency"]) if loaded.get("wallet_currency") is not None else None,
            updated_at=int(loaded["wallet_updated_at"]) if loaded.get("wallet_updated_at") is not None else None,
            source=str(loaded["wallet_source"]),
        )
        current_request_context.set(context.with_user_context(user_context))
        return user_context

    async def wallet_settings() -> dict[str, object]:
        await user_settings()
        wallet_snapshot = request_context().local.wallet
        if wallet_snapshot is None:
            raise ServiceError("wallet_unavailable", "Wallet settings are unavailable.", 503)
        return dict(wallet_snapshot.as_settings())

    async def account_steam_id() -> str:
        return request_context().principal.steam_id

    async def get_library_snapshot(
        *, fresh: bool = False, steam_id: str | None = None
    ) -> tuple[LibrarySnapshot, int, str]:
        steam_id = steam_id or await account_steam_id()
        result = await steam.get_owned_games(steam_id, force=fresh)
        fetched_at = str(result.get("_service_fetched_at", utc_now_iso()))
        games = LibrarySnapshot.build(
            [game_from_api(game) for game in result["games"]], fetched_at, int(result["game_count"])
        )
        return games, int(result["game_count"]), fetched_at

    async def observe_library_snapshot(
        *, fresh: bool = False, steam_id: str | None = None
    ) -> tuple[LibrarySnapshot, int, str]:
        user_id = user()
        games, total, fetched_at = await get_library_snapshot(fresh=fresh, steam_id=steam_id)
        await observations.observe_library(user_id, games)
        return games, total, fetched_at

    package_analysis_service = PackageAnalysisService(store.get_package_details, observe_library_snapshot)
    wishlist_service = WishlistService(
        steam.get_wishlist, db.record_wishlist_observation,
        settings.session_secret.get_secret_value().encode(),
    )

    storefront_service = StorefrontService(store, db, observations, user, user_settings)

    purchase_context_service = PurchaseContextService(
        db,
        wallet,
        observe_library_snapshot,
        storefront_service.observe_offers,
        lambda steam_id: wishlist_service.observe_snapshot(user(), steam_id),
    )


    async def load_comparison_offers(appids: list[int], user_context: UserContext) -> list[StoreOffer]:
        offers, _ = await storefront_service.observe_offers(appids, user_context=user_context)
        return offers

    async def load_context_offer(appid: int, user_context: UserContext, fresh: bool) -> StoreOffer:
        offers, _ = await storefront_service.observe_offers(
            [appid], fresh=fresh, user_context=user_context
        )
        return offers[0]

    async def load_context_metadata(appid: int, user_context: UserContext, fresh: bool) -> GameMetadata:
        return await store.get_metadata(
            appid, user_context.country_code, user_context.preferred_language, fresh=fresh
        )

    backlog_service = BacklogService(
        db.get_local_backlog, db.get_local_watchlist, db.get_price_tracking,
        lambda steam_id: observe_library_snapshot(steam_id=steam_id),
        lambda steam_id, appid, language: steam.get_game_achievements(steam_id, appid, language),
    )
    comparison_service = GameComparisonService(
        lambda steam_id: observe_library_snapshot(steam_id=steam_id), load_comparison_offers
    )
    game_context_service = GameContextService(
        lambda steam_id, fresh: observe_library_snapshot(steam_id=steam_id, fresh=fresh),
        load_context_offer,
        load_context_metadata,
        lambda steam_id, appid, language: steam.get_game_achievements(steam_id, appid, language),
        store.get_review_summary,
        steam.get_current_players,
    )
    coop_service = CoopService(
        steam.get_friend_list, steam.get_owned_games, steam.get_player_summaries,
        lambda steam_id: observe_library_snapshot(steam_id=steam_id),
    )
    library_service = LibraryService(
        lambda steam_id, fresh: get_library_snapshot(steam_id=steam_id, fresh=fresh),
        lambda steam_id, fresh: observe_library_snapshot(steam_id=steam_id, fresh=fresh),
        settings.session_secret.get_secret_value().encode(),
        db,
        steam.get_recent_games,
    )
    achievement_service = AchievementService(
        db, lambda steam_id, appid, language: steam.get_game_achievements(steam_id, appid, language)
    )
    price_history_service = PriceHistoryService(db)
    services = AppServices(
        account=AccountService(steam.get_player_summary, steam.get_steam_level),
        reviews=ReviewService(store.get_review_summary),
        capabilities=CapabilitiesService(
            full_mode=settings.mcp_toolset == "full", store_enabled=store.enabled
        ),
        observations=observations,
        backlog=backlog_service,
        game_context=game_context_service,
        comparison=comparison_service,
        coop=coop_service,
        package_analysis=package_analysis_service,
        purchase_context=purchase_context_service,
        library=library_service,
        achievements=achievement_service,
        wishlist=wishlist_service,
        price_history=price_history_service,
        storefront=storefront_service,
    )

    def output(value: Any, summary: str) -> CallToolResult:
        dumped = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            structured_content=dumped,
        )

    def strict_tool(function: Any) -> Any:
        signature = inspect.signature(function)
        fields = {
            parameter.name: (
                parameter.annotation,
                parameter.default,
            )
            for parameter in signature.parameters.values()
        }
        input_model = create_model(
            f"{function.__name__.title().replace('_', '')}Input",
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )

        @wraps(function)
        async def validated(**kwargs: Any) -> Any:
            inputs = input_model.model_validate(kwargs)
            return await function(**inputs.model_dump())

        return validated

    registration_context = RegistrationContext(
        mcp=mcp,
        services=services,
        wallet=wallet,
        user=user,
        user_context=user_settings,
        steam_id=account_steam_id,
        wallet_settings=wallet_settings,
        output=output,
        strict_tool=strict_tool,
        private=private_annotations,
        public=public_annotations,
        full_mode=settings.mcp_toolset == "full",
    )
    register_library_tools(registration_context)
    register_store_tools(registration_context)
    register_composite_tools(
        mcp, services, user_settings, account_steam_id, output, strict_tool,
        private_annotations, public_annotations, full_mode=settings.mcp_toolset == "full",
    )

    @mcp.tool(name="get_capabilities", annotations=private_annotations)
    @strict_tool
    async def get_capabilities() -> Annotated[CallToolResult, Capabilities]:
        """Check which Steam data sources are available and how fresh they are."""
        result = services.capabilities.build()
        return output(result, "Steam identity and library are connected; wallet is manually maintained.")

    register_account_tools(registration_context)
    register_prompts(mcp, full_mode=settings.mcp_toolset == "full")

    return mcp
