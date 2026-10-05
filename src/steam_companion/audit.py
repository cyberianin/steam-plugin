from __future__ import annotations

import asyncio
import json

import httpx

from steam_companion.config import get_settings
from steam_companion.steam import SteamClient


async def run_audit() -> int:
    settings = get_settings()
    steam_id = next(iter(settings.allowed_steam_id_set))
    timeout = httpx.Timeout(settings.steam_timeout_seconds, connect=settings.steam_connect_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
        client = SteamClient(
            http,
            settings.steam_web_api_key.get_secret_value(),
            settings.library_cache_ttl_seconds,
            settings.steam_timeout_seconds,
            settings.steam_connect_timeout_seconds,
        )
        try:
            library = await client.get_owned_games(steam_id, force=True)
        except Exception as error:
            code = getattr(error, "code", "audit_failed")
            print(json.dumps({"ok": False, "check": "owned_games", "error": code}))
            return 1
        try:
            recent = await client.get_recent_games(steam_id, count=0)
        except Exception as error:
            code = getattr(error, "code", "audit_failed")
            print(json.dumps({"ok": False, "check": "recent_games", "error": code}))
            return 1
    print(
        json.dumps(
            {
                "ok": True,
                "checks": {
                    "owned_games": {
                        "game_count": library["game_count"],
                        "returned_games": len(library["games"]),
                        "schema_consistent": library["game_count"] == len(library["games"]),
                    },
                    "recent_games": {
                        "total_count": recent["total_count"],
                        "returned_games": len(recent["games"]),
                        "schema_consistent": recent["total_count"] == len(recent["games"]),
                    },
                },
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_audit()))
