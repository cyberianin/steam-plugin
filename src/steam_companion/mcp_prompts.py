from __future__ import annotations

from mcp.server import MCPServer


def register_prompts(mcp: MCPServer, *, full_mode: bool) -> None:
    if not full_mode:
        return

    @mcp.prompt(
        name="what_should_i_play", title="Pick a Steam game",
        description="Use the account's library and backlog context to help choose what to play.",
    )
    def what_should_i_play(mood: str = "") -> list[dict[str, str]]:
        return [{"role": "user", "content": (
            "Help me choose a game I already own. Use get_backlog or analyze_library for compact account facts; "
            "use game_context only for a small number of finalists. Treat inferred groups as suggestions, "
            "not declared preferences. Consider this mood or constraint: " + (mood.strip() or "none given")
        )}]

    @mcp.prompt(
        name="evaluate_purchase", title="Evaluate a Steam purchase",
        description="Gather current ownership, offer, wallet and observed-history facts for a purchase decision.",
    )
    def evaluate_purchase(appids: str) -> list[dict[str, str]]:
        return [{"role": "user", "content": (
            "Evaluate these Steam candidates using facts, not an automatic buy/skip rule: " + appids + ". "
            "Resolve names to AppIDs if needed, then use check_library_games and get_purchase_context. "
            "Explain missing, stale, unofficial, manual-wallet, and cross-currency limitations."
        )}]

    @mcp.prompt(
        name="plan_game_night", title="Plan a co-op game night",
        description="Find owned candidate games shared with public friend libraries.",
    )
    def plan_game_night(appids: str) -> list[dict[str, str]]:
        return [{"role": "user", "content": (
            "Plan a co-op game night for these candidate AppIDs: " + appids + ". "
            "Use get_coop_context, respect its public-library limits, and do not infer anything about friends "
            "whose data is unavailable."
        )}]

    @mcp.prompt(
        name="sale_digest", title="Build a sale shortlist",
        description="Compare sale candidates with account ownership and regional offers.",
    )
    def sale_digest(candidates: str = "") -> list[dict[str, str]]:
        return [{"role": "user", "content": (
            "Build a concise Steam sale shortlist. Find current candidates for this request: "
            + (candidates.strip() or "ask me for a genre or budget if needed")
            + ". Verify ownership in a batch, fetch regional prices with get_store_offers, then use "
            "get_purchase_context for finalists. Label unofficial or unavailable prices and never call an "
            "observed local minimum an all-time low."
        )}]

    @mcp.prompt(
        name="review_backlog", title="Review my backlog",
        description="Summarize explicit backlog states separately from inferred activity groups.",
    )
    def review_backlog() -> list[dict[str, str]]:
        return [{"role": "user", "content": (
            "Review my Steam backlog using get_backlog. Separate my explicit states and local watchlist from "
            "inferred groups. Suggest a small set of games to revisit using playtime and recent activity, "
            "without treating inactivity as dislike or inferred status as a user-declared choice."
        )}]
