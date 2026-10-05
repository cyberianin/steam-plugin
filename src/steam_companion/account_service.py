from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from steam_companion.models import SteamAccount
from steam_companion.request_context import UserContext
from steam_companion.steam import utc_now_iso


class AccountService:
    """Builds stable account-facing models from the Steam identity and profile providers."""

    def __init__(
        self,
        load_profile: Callable[[str], Awaitable[dict[str, Any]]],
        load_level: Callable[[str], Awaitable[int | None]],
    ) -> None:
        self._load_profile = load_profile
        self._load_level = load_level

    async def account(self, user: UserContext) -> SteamAccount:
        profile_result, level_result = await asyncio.gather(
            self._load_profile(user.steam_id), self._load_level(user.steam_id),
            return_exceptions=True,
        )
        warnings: list[str] = []
        profile: dict[str, Any] = {}
        if isinstance(profile_result, BaseException):
            warnings.append("Public Steam profile details were unavailable.")
        else:
            profile = profile_result
        if isinstance(level_result, BaseException):
            warnings.append("Steam level was unavailable.")
            level = None
        else:
            level = level_result if isinstance(level_result, int) and not isinstance(level_result, bool) else None
        return SteamAccount(
            steam_id=user.steam_id,
            persona_name=profile.get("personaname") if isinstance(profile.get("personaname"), str) else None,
            profile_url=profile.get("profileurl") if isinstance(profile.get("profileurl"), str) else None,
            avatar=profile.get("avatarfull") if isinstance(profile.get("avatarfull"), str) else None,
            created_at=profile.get("timecreated") if isinstance(profile.get("timecreated"), int) else None,
            visibility_state=(profile.get("communityvisibilitystate")
                              if isinstance(profile.get("communityvisibilitystate"), int) else None),
            steam_level=level,
            store_country_code=user.country_code,
            preferred_language=user.preferred_language,
            expected_currency=user.expected_currency,
            provenance={
                "source": "Steam OpenID identity and Steam Web API public profile",
                "provider": "steam_openid+steam_web_api",
                "fetched_at": utc_now_iso(),
                "complete": not warnings,
                "truncated": False,
                "stale": False,
                "warnings": warnings,
            },
        )

    async def player_summary(self, steam_id: str) -> dict[str, Any]:
        profile = await self._load_profile(steam_id)
        return {
            "persona_name": profile.get("personaname"),
            "profile_url": profile.get("profileurl"),
            "avatar": profile.get("avatarfull"),
            "created_at": profile.get("timecreated"),
            "visibility_state": profile.get("communityvisibilitystate"),
            "provenance": {
                "source": "Steam Web API ISteamUser.GetPlayerSummaries",
                "provider": "steam_web_api",
                "fetched_at": utc_now_iso(),
                "complete": True,
                "truncated": False,
                "stale": False,
                "warnings": [],
            },
        }
