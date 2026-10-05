from __future__ import annotations

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from steam_companion.mcp_registration import RegistrationContext
from steam_companion.models import PriceHistory, PurchaseContext, ReviewSummary, StoreOffer, WalletState, WishlistPage, WishlistSummary


def register_store_tools(ctx: RegistrationContext) -> None:
    mcp, services, output, strict = ctx.mcp, ctx.services, ctx.output, ctx.strict_tool

    @mcp.tool(name="get_price_history", annotations=ctx.private)
    @strict
    async def get_price_history(
        appid: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)],
        limit: Annotated[int, Field(strict=True, ge=1, le=365)] = 90,
    ) -> Annotated[CallToolResult, PriceHistory]:
        result = await services.price_history.read(await ctx.user_context(), appid, limit)
        return output(result, f"Returned {result.returned} saved price observations for AppID {appid}.")

    @mcp.tool(name="get_review_summary", annotations=ctx.public)
    @strict
    async def get_review_summary(
        appid: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)],
        review_filter: Annotated[str, Field(pattern="^(all|recent)$")] = "all",
    ) -> Annotated[CallToolResult, ReviewSummary]:
        ctx.user()
        result = await ctx.services.reviews.summary(appid, review_filter)
        return output(result, f"Steam reports {result.review_score_description or 'an unclassified score'} for AppID {appid}.")

    @mcp.tool(name="get_wishlist_summary", annotations=ctx.private)
    @strict
    async def get_wishlist_summary() -> Annotated[CallToolResult, WishlistSummary]:
        result = await services.wishlist.observe_summary(ctx.user(), await ctx.steam_id())
        return output(result, f"Steam wishlist contains {result.total} items.")

    @mcp.tool(name="get_wishlist_games", annotations=ctx.private)
    @strict
    async def get_wishlist_games(
        cursor: Annotated[str | None, Field(max_length=512)] = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
    ) -> Annotated[CallToolResult, WishlistPage]:
        result = await services.wishlist.observe_page(ctx.user(), await ctx.steam_id(), cursor, limit)
        return output(result, f"Returned {result.returned} of {result.total} wishlist items.")

    @mcp.tool(name="get_store_offer", annotations=ctx.public)
    @strict
    async def get_store_offer(
        appid: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)], fresh: bool = False,
    ) -> Annotated[CallToolResult, StoreOffer]:
        """Fetch a current offer and record it when the AppID is locally price-tracked."""
        offers, _ = await services.storefront.observe_offers(
            [appid], user_context=await ctx.user_context(), fresh=fresh
        )
        return output(offers[0], f"Offer state for AppID {appid}: {offers[0].price_state}.")

    @mcp.tool(name="get_store_offers", annotations=ctx.public)
    @strict
    async def get_store_offers(
        appids: Annotated[list[Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]], Field(min_length=1, max_length=50)],
        fresh: bool = False,
    ) -> Annotated[CallToolResult, list[StoreOffer]]:
        """Fetch a bounded batch of offers and record observations for tracked AppIDs."""
        offers, _ = await services.storefront.observe_offers(
            appids, user_context=await ctx.user_context(), fresh=fresh
        )
        priced = sum(item.price_state in {"priced", "free"} for item in offers)
        return output(offers, f"Retrieved {priced} priced offers out of {len(offers)} AppIDs.")

    @mcp.tool(name="get_wallet_state", annotations=ctx.private)
    @strict
    async def get_wallet_state() -> Annotated[CallToolResult, WalletState]:
        state = await ctx.wallet.get_state(await ctx.wallet_settings())
        return output(state, "Wallet balance is manual." if state.available else "No wallet balance has been saved.")

    @mcp.tool(name="get_purchase_context", annotations=ctx.private)
    @strict
    async def get_purchase_context(
        appids: Annotated[list[Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]], Field(min_length=1, max_length=20)],
    ) -> Annotated[CallToolResult, PurchaseContext]:
        if len(set(appids)) != len(appids):
            from steam_companion.errors import ServiceError
            raise ServiceError("invalid_appid", "AppIDs must be unique.", 400)
        result = await services.purchase_context.build(
            await ctx.user_context(), appids, wallet_settings=await ctx.wallet_settings()
        )
        return output(result, f"Built purchase context for {len(result.items)} candidate games; no recommendation is included.")
