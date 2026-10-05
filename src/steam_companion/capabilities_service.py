from __future__ import annotations

from steam_companion.models import Capabilities
from steam_companion.providers import LocalSteamProvider


class CapabilitiesService:
    """Describes the providers wired into this single-user application instance."""

    def __init__(self, *, full_mode: bool, store_enabled: bool) -> None:
        self._full_mode = full_mode
        self._store_enabled = store_enabled

    def build(self) -> Capabilities:
        return Capabilities(
            schema_version="1",
            steam_identity={"available": True, "provider": "steam_openid", "official": True, "live": True},
            steam_account={"available": True, "provider": "ISteamUser.GetPlayerSummaries+IPlayerService.GetSteamLevel", "official": True, "live": True},
            library={"available": True, "provider": "IPlayerService.GetOwnedGames", "official": True, "live": True},
            recent_games={"available": True, "provider": "IPlayerService.GetRecentlyPlayedGames", "official": True, "live": True},
            activity_history={
                "available": self._full_mode,
                "provider": "local_confirmed_library_deltas",
                "official": False,
                "live": False,
                "observation_required": True,
            },
            achievements={
                "available": True,
                "provider": "ISteamUserStats.GetPlayerAchievements+GetSchemaForGame",
                "official": True,
                "live": True,
                "reason": "Per-game data depends on Steam exposing achievement stats.",
            },
            wishlist={
                "available": True,
                "provider": "IWishlistService.GetWishlist",
                "official": True,
                "live": True,
                "stability": "limited_static_documentation",
            },
            reviews={
                "available": True,
                "provider": "Steamworks appreviews",
                "official": True,
                "live": True,
                "review_texts_returned": False,
            },
            social={
                "available": True,
                "provider": "ISteamUser.GetFriendList+IPlayerService.GetOwnedGames",
                "official": True,
                "live": True,
                "friend_ids_returned": False,
                "visibility_required": "public_friend_list_and_public_game_details",
                "friend_limit_per_call": 20,
            },
            store_prices={
                "available": self._store_enabled,
                "provider": "Steam Storefront appdetails",
                "official": False,
                "live": self._store_enabled,
                "reason": None if self._store_enabled else "unofficial_storefront_disabled",
            },
            wallet={"available": True, "provider": "manual", "official": False, "live": False},
            local_sidecar=LocalSteamProvider.capabilities(),
        )
