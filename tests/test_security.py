from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
import aiosqlite
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from steam_companion.cimd import CimdResolver
from steam_companion.config import Settings
from steam_companion.errors import OAuthError, ServiceError
from steam_companion.oauth import OAuthService, digest, pkce_s256
from steam_companion.storage import Database
from steam_companion.steam import SteamClient
from steam_companion.mcp_server import create_mcp_server
from steam_companion.request_context import Principal, RequestContext, current_request_context
from steam_companion.pagination import SnapshotCursor
from steam_companion.store import AppDetailsSnapshot, StoreProvider
from steam_companion.models import StoreOffer
from mcp import Client


CLIENT_ID = "https://chatgpt.com/oauth/client.json"
REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
STEAM_ID = "76561198000000000"


def settings_for(path: str) -> Settings:
    key = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return Settings(
        PUBLIC_BASE_URL="https://steam.example.com",
        STEAM_WEB_API_KEY=SecretStr("test-key-never-logged"),
        ALLOWED_STEAM_IDS=STEAM_ID,
        OAUTH_SIGNING_KEY=SecretStr(key),
        SESSION_SECRET=SecretStr("x" * 64),
        DATABASE_URL=f"sqlite+aiosqlite:///{path}",
    )


class OAuthSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite+aiosqlite:///{Path(self.temp.name) / 'test.sqlite3'}")
        await self.db.initialize()
        self.metadata = {
            "client_id": CLIENT_ID,
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_methods_supported": ["none"],
        }

        async def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self.metadata)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.clients = CimdResolver(self.http)
        self.settings = settings_for(str(Path(self.temp.name) / "oauth.sqlite3"))
        self.oauth = OAuthService(self.settings, self.db, self.http, self.clients)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        self.temp.cleanup()

    async def test_cimd_allowlist_rejects_arbitrary_https_url(self) -> None:
        with self.assertRaises(OAuthError):
            await self.clients.get("https://127.0.0.1/private-metadata")
        self.assertEqual((await self.clients.get(CLIENT_ID)).redirect_uris, (REDIRECT_URI,))

    async def test_backlog_form_is_csrf_protected_and_watchlist_is_separate(self) -> None:
        user_id = "backlog-user"
        session = "long-lived-session"
        csrf = "one-time-csrf"
        async with self.db.connection() as db:
            await db.execute("INSERT INTO users(id,steam_id,created_at) VALUES(?,?,1)", (user_id, STEAM_ID))
            await db.execute(
                "INSERT INTO user_settings(user_id,store_country_code,preferred_language,expected_currency) "
                "VALUES(?,?,?,?)", (user_id, "US", "english", "USD")
            )
            await db.execute(
                "INSERT INTO management_sessions(session_hash,user_id,csrf_hash,expires_at,created_at) "
                "VALUES(?,?,?,?,?)", (digest(session), user_id, digest(csrf), 4_000_000_000, 1)
            )
            await db.commit()
        await self.oauth.save_backlog_entry(
            session_token=session, user_id=user_id, csrf_token=csrf, appid=440,
            state="want_to_play", note="later", user_score=8.5, priority=90, watched=True, track_price=True,
        )
        self.assertEqual((await self.db.get_local_backlog(user_id))[0]["state"], "want_to_play")
        self.assertEqual([row["appid"] for row in await self.db.get_local_watchlist(user_id)], [440])
        self.assertEqual([row["appid"] for row in await self.db.get_price_tracking(user_id)], [440])
        context = await self.oauth.web_session_context(session)
        self.assertTrue(context and context["management_session"])
        with self.assertRaises(ValueError):
            await self.oauth.save_backlog_entry(
                session_token=session, user_id=user_id, csrf_token=csrf, appid=441,
                state="playing", note=None, user_score=None, priority=None, watched=False, track_price=False,
            )
        self.assertEqual(len(await self.db.get_local_backlog(user_id)), 1)

    async def test_authorization_code_is_pkce_bound_and_one_time(self) -> None:
        verifier = "v" * 64
        user_id = "user-test-id"
        async with self.db.connection() as db:
            await db.execute("INSERT INTO users(id, steam_id, created_at) VALUES (?, ?, 1)", (user_id, STEAM_ID))
            await db.execute(
                "INSERT INTO authorization_codes "
                "(code_hash,user_id,client_id,redirect_uri,code_challenge,scope,resource,expires_at) "
                "VALUES (?,?,?,?,?,?,?,strftime('%s','now')+120)",
                (digest("single-use-code"), user_id, CLIENT_ID, REDIRECT_URI, pkce_s256(verifier), "steam:read", self.settings.origin),
            )
            await db.commit()

        with self.assertRaises(OAuthError):
            await self.oauth.exchange_code(
                client_id=CLIENT_ID,
                redirect_uri=REDIRECT_URI,
                code="single-use-code",
                verifier="w" * 64,
                resource=self.settings.origin,
            )
        token_response = await self.oauth.exchange_code(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code="single-use-code",
            verifier=verifier,
            resource=self.settings.origin,
        )
        claims = self.oauth.validate_access_token(str(token_response["access_token"]))
        self.assertEqual(claims["sub"], user_id)
        self.assertEqual(claims["steam_id"], STEAM_ID)
        self.assertEqual(claims["aud"], self.settings.origin)
        self.assertEqual(claims["scope"], "steam:read")
        self.assertEqual(len(str(token_response["refresh_token"])), 64)
        with self.assertRaises(OAuthError):
            await self.oauth.exchange_code(
                client_id=CLIENT_ID,
                redirect_uri=REDIRECT_URI,
                code="single-use-code",
                verifier=verifier,
                resource=self.settings.origin,
            )

    async def test_refresh_token_rotation_rejects_replay(self) -> None:
        user_id = "refresh-user"
        async with self.db.connection() as db:
            await db.execute("INSERT INTO users(id, steam_id, created_at) VALUES (?, ?, 1)", (user_id, STEAM_ID))
            await db.execute(
                "INSERT INTO refresh_tokens(token_hash,family_id,user_id,client_id,scope,resource,expires_at,created_at) "
                "VALUES (?, 'family-1', ?, ?, 'steam:read', ?, strftime('%s','now')+5000, strftime('%s','now'))",
                (digest("refresh-original"), user_id, CLIENT_ID, self.settings.origin),
            )
            await db.commit()
        rotated = await self.oauth.rotate_refresh_token(
            client_id=CLIENT_ID, refresh_token="refresh-original", resource=self.settings.origin
        )
        self.oauth.validate_access_token(str(rotated["access_token"]))
        with self.assertRaises(OAuthError):
            await self.oauth.rotate_refresh_token(
                client_id=CLIENT_ID, refresh_token="refresh-original", resource=self.settings.origin
            )
        with self.assertRaises(OAuthError):
            await self.oauth.rotate_refresh_token(
                client_id=CLIENT_ID, refresh_token=str(rotated["refresh_token"]), resource=self.settings.origin
            )

    async def test_openid_claim_requires_verified_steam_identity_fields(self) -> None:
        with self.assertRaises(OAuthError):
            self.oauth._validate_openid_fields(
                {
                    "openid.ns": "http://specs.openid.net/auth/2.0",
                    "openid.mode": "id_res",
                    "openid.op_endpoint": "https://attacker.example/",
                    "openid.return_to": "https://steam.example.com/auth/steam/callback?state=fake",
                    "openid.identity": f"https://steamcommunity.com/openid/id/{STEAM_ID}",
                    "openid.claimed_id": f"https://steamcommunity.com/openid/id/{STEAM_ID}",
                    "openid.signed": "op_endpoint,claimed_id,identity,return_to,response_nonce",
                    "openid.response_nonce": "2026-10-01T00:00:00Znonce",
                },
                "https://steam.example.com/auth/steam/callback?state=real",
            )


class SteamClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_cache_evicts_oldest_entries_at_fixed_capacity(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            steam = SteamClient(client, "secret")

            async def value(number: int) -> dict[str, int]:
                return {"value": number}

            for number in range(steam.CACHE_MAX_ENTRIES + 20):
                await steam._cached(f"key:{number}", 60, lambda number=number: value(number))

        self.assertEqual(len(steam._cache), steam.CACHE_MAX_ENTRIES)
        self.assertNotIn("key:0", steam._cache)
        self.assertIn(f"key:{steam.CACHE_MAX_ENTRIES + 19}", steam._cache)

    async def test_friend_list_is_normalized_and_private_visibility_is_distinct(self) -> None:
        friend_id = "76561198000000001"

        async def success(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params.get("relationship"), "friend")
            return httpx.Response(200, json={"friendslist": {"friends": [
                {"steamid": friend_id, "relationship": "friend", "friend_since": 1700000000},
                {"steamid": "invalid", "relationship": "friend", "friend_since": 1},
            ]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
            steam = SteamClient(client, "secret")
            friends = await steam.get_friend_list(STEAM_ID)
        self.assertEqual(friends, [{"steamid": friend_id, "friend_since": 1700000000}])

        async def private(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        async with httpx.AsyncClient(transport=httpx.MockTransport(private)) as client:
            steam = SteamClient(client, "secret")
            with self.assertRaises(ServiceError) as caught:
                await steam.get_friend_list(STEAM_ID)
        self.assertEqual(caught.exception.code, "friend_list_private")

    async def test_wishlist_is_validated_and_normalized(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            self.assertIn("IWishlistService/GetWishlist/v1/", request.url.path)
            return httpx.Response(200, json={"response": {"items": [
                {"appid": 10, "priority": 0, "date_added": 1700000000},
                {"appid": 20, "priority": 1, "date_added": 0},
            ]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            steam = SteamClient(client, "secret")
            result = await steam.get_wishlist(STEAM_ID)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["items"][0]["appid"], 10)
        self.assertIsNone(result["items"][1]["date_added"])

    async def test_missing_wishlist_collection_is_not_treated_as_empty(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"response": {}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            steam = SteamClient(client, "secret")
            with self.assertRaises(ServiceError) as caught:
                await steam.get_wishlist(STEAM_ID)
        self.assertEqual(caught.exception.code, "wishlist_unavailable")

    async def test_achievement_summary_joins_official_schema_and_account_state(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            if "GetSchemaForGame" in request.url.path:
                return httpx.Response(200, json={"game": {"gameName": "Game", "availableGameStats": {"achievements": [
                    {"name": "FIRST", "displayName": "First steps", "description": "Start", "hidden": 0},
                    {"name": "SECRET", "displayName": "Secret", "description": "Spoiler", "hidden": 1},
                ]}}})
            return httpx.Response(200, json={"playerstats": {"success": True, "achievements": [
                {"apiname": "FIRST", "achieved": 1, "unlocktime": 1700000000},
                {"apiname": "SECRET", "achieved": 0, "unlocktime": 0},
            ]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            steam = SteamClient(client, "secret")
            result = await steam.get_game_achievements(STEAM_ID, 10)
        definitions = {item["name"]: item for item in result["definitions"]}
        states = {item["apiname"]: item for item in result["player_achievements"]}
        self.assertEqual(result["game_name"], "Game")
        self.assertEqual(len(definitions), 2)
        self.assertTrue(states["FIRST"]["achieved"])
        self.assertFalse(states["SECRET"]["achieved"])

    async def test_duplicate_concurrent_misses_share_one_upstream_request(self) -> None:
        calls = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return httpx.Response(
                200,
                json={"response": {"game_count": 1, "games": [{"appid": 10, "name": "Game", "playtime_forever": 12}]}},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            steam = SteamClient(client, "secret", library_ttl=30)
            results = await asyncio.gather(*(steam.get_owned_games(STEAM_ID) for _ in range(20)))
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 20)
        self.assertTrue(all(item == results[0] for item in results))

    async def test_concurrent_fresh_reads_share_one_refresh_generation(self) -> None:
        calls = 0

        async def load() -> dict[str, int]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {"generation": calls}

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            steam = SteamClient(client, "secret")
            await steam._cached("shared", 60, load)
            refreshed = await asyncio.gather(*(steam._cached("shared", 60, load, fresh=True) for _ in range(20)))

        self.assertEqual(calls, 2)
        self.assertEqual({item["generation"] for item in refreshed}, {2})

    async def test_cancelled_cache_waiter_does_not_leak_inflight_entry(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def load() -> dict[str, int]:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"value": 1}

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            steam = SteamClient(client, "secret")
            waiter = asyncio.create_task(steam._cached("cancelled", 60, load))
            await started.wait()
            other_waiter = asyncio.create_task(steam._cached("cancelled", 60, load))
            await asyncio.sleep(0)
            loader = steam._inflight["cancelled"]
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            await loader
            self.assertEqual((await other_waiter)["value"], 1)
            for _ in range(3):
                if "cancelled" not in steam._inflight:
                    break
                await asyncio.sleep(0)

            self.assertNotIn("cancelled", steam._inflight)
            self.assertEqual(steam._cache["cancelled"][1]["value"], 1)
            self.assertEqual(calls, 1)

    async def test_cache_loader_exception_clears_inflight_and_allows_retry(self) -> None:
        calls = 0

        async def load() -> dict[str, int]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary upstream failure")
            return {"value": 2}

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            steam = SteamClient(client, "secret")
            with self.assertRaisesRegex(RuntimeError, "temporary upstream failure"):
                await steam._cached("retry", 60, load)
            self.assertNotIn("retry", steam._inflight)
            self.assertEqual((await steam._cached("retry", 60, load))["value"], 2)

        self.assertEqual(calls, 2)

    async def test_private_or_partial_library_is_not_reported_as_empty(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"response": {"game_count": 0}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            steam = SteamClient(client, "secret")
            with self.assertRaises(ServiceError) as caught:
                await steam.get_owned_games(STEAM_ID)
        self.assertEqual(caught.exception.code, "library_private")


class McpContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_review_and_capabilities_tool_handlers_delegate_to_services(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("GetPlayerSummaries/v2/"):
                return httpx.Response(200, json={"response": {"players": [{
                    "steamid": STEAM_ID, "personaname": "Test Player",
                    "profileurl": "https://steamcommunity.com/profiles/test/",
                    "avatarfull": "https://example.test/avatar.png", "timecreated": 123,
                    "communityvisibilitystate": 3,
                }]}})
            if request.url.path.endswith("GetSteamLevel/v1/"):
                return httpx.Response(200, json={"response": {"player_level": 9}})
            if request.url.path == "/appreviews/10":
                return httpx.Response(200, json={"success": 1, "query_summary": {
                    "review_score": 8, "review_score_desc": "Very Positive", "total_reviews": 10,
                    "total_positive": 8, "total_negative": 2,
                }})
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "account-services.sqlite3"
            db = Database(f"sqlite+aiosqlite:///{db_path}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES('account-user',?,1)", (STEAM_ID,))
                await conn.execute(
                    "INSERT INTO user_settings(user_id,store_country_code,preferred_language,expected_currency) "
                    "VALUES('account-user','US','english','USD')"
                )
                await conn.commit()
            settings = settings_for(str(db_path))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                oauth = OAuthService(settings, db, http, CimdResolver(http))
                server = create_mcp_server(
                    settings, db, oauth, SteamClient(http, "test-key"), StoreProvider(http, enabled=False)
                )
                async with Client(server) as client:
                    marker = current_request_context.set(RequestContext(Principal("account-user", STEAM_ID)))
                    try:
                        account = await client.call_tool("get_steam_account", {})
                        review = await client.call_tool("get_review_summary", {"appid": 10})
                        capabilities = await client.call_tool("get_capabilities", {})
                    finally:
                        current_request_context.reset(marker)

        self.assertEqual(account.structured_content["persona_name"], "Test Player")
        self.assertEqual(account.structured_content["steam_level"], 9)
        self.assertEqual(review.structured_content["positive_percent"], 80.0)
        self.assertFalse(capabilities.structured_content["store_prices"]["available"])

    async def test_purchase_context_batches_and_joins_watchlists_and_price_history(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            if "GetOwnedGames" in request.url.path:
                return httpx.Response(200, json={"response": {"game_count": 1, "games": [{"appid": 10, "playtime_forever": 60}]}})
            if "GetWishlist" in request.url.path:
                return httpx.Response(200, json={"response": {"items": [{"appid": 11, "priority": 1}]}})
            return httpx.Response(200, json={})

        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "purchase.sqlite3"
            db = Database(f"sqlite+aiosqlite:///{db_path}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES('purchase-user',?,1)", (STEAM_ID,))
                await conn.execute(
                    "INSERT INTO user_settings(user_id,store_country_code,preferred_language,expected_currency) "
                    "VALUES('purchase-user','US','english','USD')"
                )
                await conn.execute(
                    "INSERT INTO game_identity(id,provider,provider_game_id,steam_appid) VALUES('steam:12','steam','12',12)"
                )
                await conn.execute(
                    "INSERT INTO game_identity(id,provider,provider_game_id,steam_appid) VALUES('steam:10','steam','10',10)"
                )
                await conn.execute(
                    "INSERT INTO watchlist(user_id,game_id,added_at) VALUES('purchase-user','steam:12','2026-10-01T00:00:00Z')"
                )
                await conn.execute(
                    "INSERT INTO price_tracking(user_id,game_id,added_at) VALUES('purchase-user','steam:10','2026-10-01T00:00:00Z')"
                )
                await conn.commit()
            await db.record_price_snapshots([
                (10, "US", "USD", "priced", 1000, 800, 20, "2026-10-01T00:00:00Z", "test")
            ])
            tracking_reads = 0
            get_price_tracking = db.get_price_tracking

            async def counted_price_tracking(user_id: str) -> list[dict[str, object]]:
                nonlocal tracking_reads
                tracking_reads += 1
                return await get_price_tracking(user_id)

            db.get_price_tracking = counted_price_tracking  # type: ignore[method-assign]
            settings = settings_for(str(db_path))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                oauth = OAuthService(settings, db, http, CimdResolver(http))
                settings_fetches = 0
                get_user_settings = oauth.get_user_settings

                async def counted_settings(user_id: str) -> dict[str, object]:
                    nonlocal settings_fetches
                    settings_fetches += 1
                    return await get_user_settings(user_id)

                oauth.get_user_settings = counted_settings  # type: ignore[method-assign]
                server = create_mcp_server(settings, db, oauth, SteamClient(http, "test-key"), StoreProvider(http, enabled=False))
                async with Client(server) as client:
                    marker = current_request_context.set(RequestContext(Principal("purchase-user", STEAM_ID)))
                    try:
                        result = await client.call_tool("get_purchase_context", {"appids": [10, 11]})
                        purchase_settings_fetches = settings_fetches
                        purchase_tracking_reads = tracking_reads
                        untracked_history = await client.call_tool("get_price_history", {"appid": 11})
                    finally:
                        current_request_context.reset(marker)
        data = result.structured_content
        self.assertEqual(data["steam_wishlist_appids"], [11])
        self.assertEqual(data["local_watchlist_appids"], [12])
        self.assertEqual(data["recent_price_history"]["10"][0]["final_price_minor"], 800)
        self.assertEqual(data["price_tracking_appids"], [10])
        self.assertEqual(data["items"][0]["ownership"], "owned")
        self.assertEqual(purchase_settings_fetches, 1)
        self.assertEqual(purchase_tracking_reads, 1)
        self.assertFalse(untracked_history.is_error)
        self.assertEqual(untracked_history.structured_content["returned"], 0)

    async def test_package_analysis_counts_visible_owned_duplicates_without_false_not_owned(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            if "GetOwnedGames" in request.url.path:
                return httpx.Response(200, json={"response": {"game_count": 1, "games": [
                    {"appid": 10, "name": "Owned game", "playtime_forever": 60},
                ]}})
            if request.url.path.endswith("/api/packagedetails"):
                return httpx.Response(200, json={"123": {"success": True, "data": {
                    "name": "Example bundle",
                    "apps": [{"id": 10, "name": "Owned game"}, {"id": 11, "name": "Entitlement unknown"}],
                    "price": {"initial": 3000, "final": 2000, "currency": "USD"},
                }}})
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "package.sqlite3"
            db = Database(f"sqlite+aiosqlite:///{db_path}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES('package-user',?,1)", (STEAM_ID,))
                await conn.execute(
                    "INSERT INTO user_settings(user_id,store_country_code,preferred_language,expected_currency) "
                    "VALUES('package-user','US','english','USD')"
                )
                await conn.commit()
            settings = settings_for(str(db_path)).model_copy(update={"mcp_toolset": "full"})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                oauth = OAuthService(settings, db, http, CimdResolver(http))
                server = create_mcp_server(
                    settings, db, oauth, SteamClient(http, "test-key"), StoreProvider(http, enabled=True)
                )
                async with Client(server) as client:
                    marker = current_request_context.set(RequestContext(Principal("package-user", STEAM_ID)))
                    try:
                        result = await client.call_tool("analyze_package", {"package_id": 123})
                    finally:
                        current_request_context.reset(marker)

        package = result.structured_content
        self.assertEqual(package["duplicate_owned_count"], 1)
        self.assertEqual(package["owned_appids"], [10])
        self.assertIsNone(package["apps"][1]["owned"])
        self.assertEqual(package["apps"][1]["ownership"], "unknown")
        self.assertEqual(package["unknown_item_count"], 1)
        self.assertIsNone(package["fully_covered"])
        self.assertEqual(package["final_price_minor"], 2000)

    async def test_core_surface_is_compact_and_never_accepts_a_steam_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'contracts.sqlite3'}")
            await db.initialize()
            settings = settings_for(str(Path(temp) / "contracts.sqlite3"))
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as http:
                clients = CimdResolver(http)
                oauth = OAuthService(settings, db, http, clients)
                steam = SteamClient(http, "test-key")
                store = StoreProvider(http, enabled=False)
                server = create_mcp_server(settings, db, oauth, steam, store)
                async with Client(server) as client:
                    tools = (await client.list_tools()).tools
                full_server = create_mcp_server(settings.model_copy(update={"mcp_toolset": "full"}), db, oauth, steam, store)
                async with Client(full_server) as client:
                    full_tool_names = {tool.name for tool in (await client.list_tools()).tools
                    }
                    full_prompt_names = {prompt.name for prompt in (await client.list_prompts()).prompts}
                    purchase_prompt = await client.get_prompt("evaluate_purchase", {"appids": "10, 20"})
        names = {tool.name for tool in tools}
        self.assertEqual(
            names,
            {
                "get_capabilities", "get_steam_account", "get_library_summary", "analyze_library", "game_context",
                "list_library_games", "get_library_game", "check_library_games", "get_recent_games",
                "get_achievement_summary",
                "get_wishlist_summary", "get_wishlist_games",
                "get_library_history",
                "get_backlog",
                "get_price_history",
                "get_review_summary",
                "get_coop_context",
                "get_store_offer", "get_store_offers", "get_wallet_state", "get_purchase_context",
            },
        )
        for tool in tools:
            self.assertNotIn("steam_id", tool.input_schema.get("properties", {}))
            self.assertTrue(tool.annotations and tool.annotations.read_only_hint)
        list_schema = next(tool.input_schema for tool in tools if tool.name == "list_library_games")
        self.assertEqual(list_schema["properties"]["limit"].get("default"), 50)
        self.assertIn(
            {"type": "string", "maxLength": 512},
            list_schema["properties"]["cursor"]["anyOf"],
        )
        check_schema = next(tool.input_schema for tool in tools if tool.name == "check_library_games")
        self.assertEqual(check_schema["properties"]["appids"]["maxItems"], 100)
        self.assertEqual(check_schema["properties"]["appids"]["items"]["maximum"], 2_147_483_647)
        self.assertIn("get_activity_history", full_tool_names)
        self.assertIn("get_event_history", full_tool_names)
        self.assertIn("analyze_package", full_tool_names)
        self.assertIn("compare_games", full_tool_names)
        self.assertEqual(
            full_prompt_names,
            {"what_should_i_play", "evaluate_purchase", "plan_game_night", "sale_digest", "review_backlog"},
        )
        self.assertIn("10, 20", purchase_prompt.messages[0].content.text)

    async def test_library_cursor_is_signed_and_bound_to_user_and_filters(self) -> None:
        filters = {"query": "portal", "played": "played", "sort": "name"}
        token = SnapshotCursor.encode(75, "user-a", filters, b"s" * 32, "snapshot-1", "library")
        self.assertEqual(SnapshotCursor.decode(token, "user-a", filters, b"s" * 32, "snapshot-1", "library"), 75)
        with self.assertRaises(ServiceError):
            SnapshotCursor.decode(token, "user-b", filters, b"s" * 32, "snapshot-1", "library")
        with self.assertRaises(ServiceError):
            SnapshotCursor.decode(token, "user-a", {**filters, "sort": "playtime_desc"}, b"s" * 32, "snapshot-1", "library")
        with self.assertRaises(ServiceError) as stale:
            SnapshotCursor.decode(token, "user-a", filters, b"s" * 32, "snapshot-2", "library")
        self.assertEqual(stale.exception.code, "cursor_stale")

    async def test_library_listing_fetches_full_collection_then_filters_and_pages_locally(self) -> None:
        upstream_calls = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            if "GetOwnedGames" not in request.url.path:
                return httpx.Response(404)
            upstream_calls += 1
            games = [
                {
                    "appid": appid,
                    "name": f"Game {appid:04}",
                    "playtime_forever": appid,
                    "rtime_last_played": 1700000000 + appid,
                }
                for appid in range(1, 2501)
            ]
            return httpx.Response(200, json={"response": {"game_count": len(games), "games": games}})

        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "library.sqlite3"
            db = Database(f"sqlite+aiosqlite:///{db_path}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES ('library-user',?,1)", (STEAM_ID,))
                await conn.commit()
            settings = settings_for(str(db_path))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                oauth = OAuthService(settings, db, http, CimdResolver(http))
                steam = SteamClient(http, "test-key")
                server = create_mcp_server(settings, db, oauth, steam, StoreProvider(http, enabled=False))
                async with Client(server) as client:
                    marker = current_request_context.set(RequestContext(Principal("library-user", STEAM_ID)))
                    try:
                        first = await client.call_tool("list_library_games", {
                            "played": "played", "sort": "playtime_desc", "limit": 2,
                        })
                        first_data = first.structured_content
                        second = await client.call_tool("list_library_games", {
                            "played": "played", "sort": "playtime_desc", "limit": 2,
                            "cursor": first_data["next_cursor"],
                        })
                        second_data = second.structured_content
                        cached_games = steam._cache[f"owned:{STEAM_ID}"][1]["games"]
                        cached_appids = [item["appid"] for item in cached_games]
                    finally:
                        current_request_context.reset(marker)
        self.assertEqual(upstream_calls, 1)
        self.assertEqual(first_data["total_matching"], 2500)
        self.assertEqual([game["appid"] for game in first_data["games"]], [2500, 2499])
        self.assertEqual([game["appid"] for game in second_data["games"]], [2498, 2497])
        self.assertTrue(first_data["complete"])
        self.assertEqual(cached_appids, list(range(1, 2501)))


class StorageHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_busy_wait_does_not_block_the_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'contention.sqlite3'}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES ('busy-user',?,1)", (STEAM_ID,))
                await conn.commit()
            blocker = await db.connect()
            await blocker.execute("BEGIN IMMEDIATE")
            waiting_write = asyncio.create_task(db.record_library_observation(
                "busy-user", "2026-10-01", 1, 1, 60, "2026-10-01T10:00:00Z", [(10, 60)]
            ))

            async def release_competing_transaction() -> None:
                await asyncio.sleep(0.15)
                await blocker.commit()

            release = asyncio.create_task(release_competing_transaction())
            ticks = 0
            started = asyncio.get_running_loop().time()
            while not waiting_write.done() and asyncio.get_running_loop().time() - started < 2:
                ticks += 1
                await asyncio.sleep(0.005)
            await asyncio.gather(waiting_write, release)
            await blocker.close()
            self.assertGreaterEqual(ticks, 10)

    async def test_schema_migrates_and_daily_snapshot_upserts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'history.sqlite3'}")
            await db.initialize()
            await db.initialize()
            async with db.connection() as conn:
                cursor = await conn.execute("PRAGMA user_version")
                version = (await cursor.fetchone())[0]
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES ('history-user',?,1)", (STEAM_ID,))
                await conn.commit()
            await db.record_library_snapshot("history-user", "2026-10-01", 10, 4, 300, "2026-10-01T10:00:00Z")
            await db.record_library_snapshot("history-user", "2026-10-01", 11, 5, 320, "2026-10-01T11:00:00Z")
            await db.record_price_snapshot(10, "KZ", "KZT", "priced", 1000, 700, 30,
                                            "2026-10-01T10:00:00Z", "steam_storefront_appdetails")
            await db.record_price_snapshot(10, "KZ", "KZT", "priced", 1000, 700, 30,
                                            "2026-10-01T10:00:00Z", "steam_storefront_appdetails")
            rows = await db.get_library_snapshots("history-user", 10)
            prices = await db.get_price_snapshots(10, "KZ", "KZT", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(version, 8)
        self.assertEqual(rows[0]["total_games"], 11)
        self.assertEqual(rows[0]["total_playtime_minutes"], 320)
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0]["final_price_minor"], 700)

    async def test_price_history_batch_groups_latest_points_per_appid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'price-batch.sqlite3'}")
            await db.initialize()
            await db.record_price_snapshots([
                (10, "US", "USD", "priced", 1000, 900, 10, "2026-10-01T10:00:00Z", "test"),
                (10, "US", "USD", "priced", 1000, 700, 30, "2026-10-02T10:00:00Z", "test"),
                (11, "US", "USD", "free", None, 0, 0, "2026-10-02T10:00:00Z", "test"),
            ])
            grouped = await db.get_price_snapshots_batch([10, 11], "US", "USD", 1)
        self.assertEqual([row["final_price_minor"] for row in grouped[10]], [700])
        self.assertEqual([row["appid"] for row in grouped[11]], [11])

    async def test_activity_observation_records_only_confirmed_positive_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'activity.sqlite3'}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES ('activity-user',?,1)", (STEAM_ID,))
                await conn.commit()

            await db.record_library_observation(
                "activity-user", "2026-10-01", 2, 1, 100, "2026-10-01T10:00:00Z", [(10, 100), (20, 0)]
            )
            self.assertEqual(await db.get_activity_deltas("activity-user", 20), [])
            with self.assertRaises(aiosqlite.IntegrityError):
                await db.record_library_observation(
                    "activity-user", "2026-10-01", 99, 99, 999, "2026-10-01T10:30:00Z", [(30, -1)]
                )
            snapshots = await db.get_library_snapshots("activity-user", 10)
            self.assertEqual(snapshots[0]["total_games"], 2)
            await db.record_library_observation(
                "activity-user", "2026-10-01", 2, 2, 135, "2026-10-01T11:00:00Z", [(10, 130), (20, 5)]
            )
            await db.record_library_observation(
                "activity-user", "2026-10-01", 2, 2, 135, "2026-10-01T12:00:00Z", [(10, 130), (20, 5)]
            )
            await db.record_library_observation(
                "activity-user", "2026-10-01", 2, 1, 110, "2026-10-01T13:00:00Z", [(10, 105), (20, 5)]
            )
            await db.record_library_observation(
                "activity-user", "2026-10-01", 2, 1, 115, "2026-10-01T14:00:00Z", [(10, 110), (20, 5)]
            )
            deltas = await db.get_activity_deltas("activity-user", 20)
            events = await db.get_event_journal("activity-user", 20)

        self.assertEqual([row["delta_minutes"] for row in deltas], [5, 5, 30])
        self.assertEqual(deltas[0]["previous_playtime_minutes"], 105)
        self.assertNotIn("game_acquired", [row["event_type"] for row in events])
        self.assertEqual(sum(row["event_type"] == "game_first_played" for row in events), 1)
        self.assertEqual(sum(row["event_type"] == "playtime_changed" for row in events), 3)

    async def test_library_event_journal_baseline_addition_and_atomic_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'events.sqlite3'}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES ('event-user',?,1)", (STEAM_ID,))
                await conn.commit()

            await db.record_library_observation(
                "event-user", "2026-10-01", 0, 0, 0, "2026-10-01T10:00:00Z", []
            )
            self.assertEqual(await db.get_event_journal("event-user", 20), [])
            await db.record_library_observation(
                "event-user", "2026-10-01", 1, 0, 0, "2026-10-01T11:00:00Z", [(42, 0)]
            )
            events = await db.get_event_journal("event-user", 20)
            self.assertEqual([row["event_type"] for row in events], ["game_acquired"])

            with self.assertRaises(aiosqlite.IntegrityError):
                await db.record_library_observation(
                    "event-user", "2026-10-01", 2, 1, 10, "2026-10-01T12:00:00Z", [(42, 10), (99, -1)]
                )
            events_after_rollback = await db.get_event_journal("event-user", 20)
            self.assertEqual(events_after_rollback, events)
            competing_db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'events.sqlite3'}")
            await asyncio.gather(
                db.record_library_observation(
                    "event-user", "2026-10-01", 1, 1, 15, "2026-10-01T13:00:00Z", [(42, 15)]
                ),
                competing_db.record_library_observation(
                    "event-user", "2026-10-01", 1, 1, 20, "2026-10-01T14:00:00Z", [(42, 20)]
                ),
            )
            concurrent_deltas = await db.get_activity_deltas("event-user", 20)
            self.assertEqual(sum(int(row["delta_minutes"]) for row in concurrent_deltas), 20)
            latest_events = await db.get_event_journal("event-user", 20)
            self.assertLessEqual(sum(row["event_type"] == "game_first_played" for row in latest_events), 1)
            async with db.connection() as conn:
                cursor = await conn.execute(
                    "SELECT playtime_minutes FROM library_presence WHERE user_id='event-user' AND appid=42"
                )
                self.assertEqual((await cursor.fetchone())[0], 20)

    async def test_wishlist_and_achievement_events_require_a_prior_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'social-events.sqlite3'}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES ('social-user',?,1)", (STEAM_ID,))
                await conn.commit()

            await db.record_wishlist_observation("social-user", [10], "2026-10-01T10:00:00Z")
            await db.record_achievement_observation("social-user", 10, ["ACH_FIRST"], "2026-10-01T10:00:00Z")
            self.assertEqual(await db.get_event_journal("social-user", 20), [])
            await db.record_wishlist_observation("social-user", [10, 20], "2026-10-01T11:00:00Z")
            await db.record_achievement_observation(
                "social-user", 10, ["ACH_FIRST", "ACH_SECOND"], "2026-10-01T11:00:00Z"
            )
            await db.record_wishlist_observation("social-user", [20], "2026-10-01T12:00:00Z")
            events = await db.get_event_journal("social-user", 20)
        self.assertEqual(
            [(row["event_type"], row["appid"]) for row in reversed(events)],
            [("wishlist_added", 20), ("achievement_unlocked", 10), ("wishlist_removed", 10)],
        )
        self.assertEqual(events[1]["payload"], {"api_names": ["ACH_SECOND"]})

    async def test_price_observations_emit_only_confirmed_tracked_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'price-events.sqlite3'}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES ('price-user',?,1)", (STEAM_ID,))
                await conn.execute(
                    "INSERT INTO game_identity(id,provider,provider_game_id,steam_appid) VALUES('steam:10','steam','10',10)"
                )
                await conn.execute(
                    "INSERT INTO price_tracking(user_id,game_id,added_at) VALUES('price-user','steam:10','2026-10-01T00:00:00Z')"
                )
                await conn.commit()

            def point(price: int, discount: int, fetched_at: str) -> tuple[int, str, str, str, int, int, int, str, str]:
                return (10, "US", "USD", "priced", 1000, price, discount, fetched_at, "test")

            await db.record_price_snapshots([point(1000, 0, "2026-10-01T10:00:00Z")])
            self.assertEqual(await db.get_event_journal("price-user", 20), [])
            await db.record_price_snapshots([point(800, 20, "2026-10-01T11:00:00Z")])
            await db.record_price_snapshots([point(700, 30, "2026-10-01T12:00:00Z")])
            await db.record_price_snapshots([point(800, 0, "2026-10-01T13:00:00Z")])
            events = await db.get_event_journal("price-user", 20)
        event_types = [row["event_type"] for row in events]
        self.assertEqual(event_types.count("price_changed"), 3)
        self.assertEqual(event_types.count("sale_started"), 1)
        self.assertEqual(event_types.count("new_personal_observed_low"), 2)


class StoreReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_store_waiters_finalize_shared_loaders(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_offer(appid: int, country: str, language: str, currency: str) -> StoreOffer:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return StoreOffer(
                appid=appid, country_code=country, price_state="free", store_url="https://store.steampowered.com/app/10/",
                fetched_at="2026-10-01T00:00:00Z", source="test", source_stability="unofficial",
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            provider = StoreProvider(client, enabled=False)
            provider._fetch = fake_offer  # type: ignore[method-assign]
            waiter = asyncio.create_task(provider.get_offer(10, "US", "english", "USD"))
            await started.wait()
            other_waiter = asyncio.create_task(provider.get_offer(10, "US", "english", "USD"))
            await asyncio.sleep(0)
            key = (10, "US", "english", "USD")
            loader = provider._inflight[key]
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            await loader
            self.assertEqual((await other_waiter).price_state, "free")
            for _ in range(3):
                if key not in provider._inflight:
                    break
                await asyncio.sleep(0)

            self.assertNotIn(key, provider._inflight)
            self.assertEqual(provider._cache[key][1].price_state, "free")
            self.assertEqual(calls, 1)

    async def test_appdetails_loader_exception_does_not_poison_retry(self) -> None:
        calls = 0

        async def load(appid: int, country: str, language: str) -> AppDetailsSnapshot:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary appdetails failure")
            return AppDetailsSnapshot(appid, country, language, "2026-10-01T00:00:00Z", {"name": "Game"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            provider = StoreProvider(client, enabled=True)
            provider._load_appdetails = load  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "temporary appdetails failure"):
                await provider._appdetails(10, "US", "english")
            self.assertNotIn((10, "US", "english"), provider._appdetails_inflight)
            snapshot = await provider._appdetails(10, "US", "english")

        self.assertEqual(snapshot.data["name"], "Game")
        self.assertEqual(calls, 2)

    async def test_offer_and_metadata_share_one_cached_appdetails_snapshot(self) -> None:
        appdetails_calls = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            nonlocal appdetails_calls
            if request.url.path == "/api/appdetails":
                appdetails_calls += 1
                return httpx.Response(200, json={"10": {"success": True, "data": {
                    "name": "Example Game", "type": "game", "is_free": False,
                    "price_overview": {"currency": "USD", "initial": 2000, "final": 1000, "discount_percent": 50},
                    "release_date": {"date": "12 Sep, 2024", "coming_soon": False},
                    "developers": ["Studio"], "publishers": ["Publisher"],
                    "genres": [{"description": "Adventure"}],
                    "categories": [{"description": "Single-player"}, {"description": "Online Co-op"}],
                    "platforms": {"windows": True, "mac": False, "linux": True},
                    "controller_support": "full", "dlc": [11], "packages": [22],
                    "metacritic": {"score": 87}, "supported_languages": "English<strong>*</strong>, French",
                    "steam_deck_compatibility": {"category": 3},
                }}})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = StoreProvider(client, enabled=True, ttl=30)
            offer, metadata = await asyncio.gather(
                provider.get_offer(10, "US", "english", "USD"),
                provider.get_metadata(10, "US", "english"),
            )
            await provider.get_metadata(10, "US", "english")
            await provider.get_offer(10, "US", "english", "USD", fresh=True)

        self.assertEqual(appdetails_calls, 2)
        self.assertEqual(offer.final_price_minor, 1000)
        self.assertEqual(metadata.genres, ["Adventure"])
        self.assertEqual(metadata.coop, True)
        self.assertEqual(metadata.steam_deck, "verified")
        self.assertEqual(metadata.dlc_appids, [11])

    async def test_fresh_offer_requests_coalesce_and_cache_stays_bounded(self) -> None:
        calls = 0
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            provider = StoreProvider(client, enabled=False, ttl=30)

            async def fetch(appid: int, country: str, language: str, currency: str) -> StoreOffer:
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.01)
                return StoreOffer(
                    appid=appid, country_code=country, price_state="provider_error", store_url=f"https://store.steampowered.com/app/{appid}/",
                    fetched_at="2026-10-01T00:00:00Z", source="test", source_stability="unofficial",
                )

            provider._fetch = fetch  # type: ignore[method-assign]
            await provider.get_offer(1, "US", "english", "USD")
            results = await asyncio.gather(*(
                provider.get_offer(1, "US", "english", "USD", fresh=True) for _ in range(20)
            ))

            for appid in range(2, provider.OFFER_CACHE_MAX_ENTRIES + 10):
                await provider.get_offer(appid, "US", "english", "USD")

        self.assertEqual(calls, 1 + 1 + provider.OFFER_CACHE_MAX_ENTRIES + 8)
        self.assertTrue(all(result.appid == 1 for result in results))
        self.assertLessEqual(len(provider._cache), provider.OFFER_CACHE_MAX_ENTRIES)
        self.assertFalse(provider._inflight)

    async def test_review_summary_uses_aggregate_only_and_coalesces_cache(self) -> None:
        calls = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.url.path, "/appreviews/10")
            self.assertEqual(request.url.params.get("filter"), "recent")
            return httpx.Response(200, json={
                "success": 1,
                "query_summary": {
                    "review_score": 8,
                    "review_score_desc": "Very Positive",
                    "total_reviews": 100,
                    "total_positive": 80,
                    "total_negative": 20,
                },
                "reviews": [{"review": "must not be returned"}],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = StoreProvider(client, enabled=False, ttl=30)
            first, second = await asyncio.gather(
                provider.get_review_summary(10, "recent"),
                provider.get_review_summary(10, "recent"),
            )
        self.assertEqual(calls, 1)
        self.assertEqual(first["positive_reviews"], 80)
        self.assertNotIn("reviews", first)
        self.assertEqual(first, second)


class PersonalExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_contains_normalized_personal_data_but_no_auth_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "export.sqlite3"
            settings = settings_for(str(db_path))
            with patch.dict(os.environ, {
                "PUBLIC_BASE_URL": settings.public_base_url,
                "STEAM_WEB_API_KEY": settings.steam_web_api_key.get_secret_value(),
                "ALLOWED_STEAM_IDS": settings.allowed_steam_ids,
                "OAUTH_SIGNING_KEY": settings.oauth_signing_key.get_secret_value(),
                "SESSION_SECRET": settings.session_secret.get_secret_value(),
                "DATABASE_URL": settings.database_url,
            }):
                from steam_companion.app import create_app

            app = create_app(settings)
            token = "private-management-session"
            async with app.router.lifespan_context(app):
                db: Database = app.state.database
                user_id = "export-user"
                async with db.connection() as conn:
                    await conn.execute(
                        "INSERT INTO users(id,steam_id,created_at) VALUES(?,?,1)", (user_id, STEAM_ID)
                    )
                    await conn.execute(
                        "INSERT INTO user_settings(user_id,store_country_code,preferred_language,expected_currency) "
                        "VALUES(?,'US','english','USD')", (user_id,)
                    )
                    await conn.execute(
                        "INSERT INTO management_sessions(session_hash,user_id,csrf_hash,expires_at,created_at) "
                        "VALUES(?,?,?,4102444800,1)", (digest(token), user_id, digest("csrf"))
                    )
                    await conn.execute(
                        "INSERT INTO game_identity(id,provider,provider_game_id,steam_appid) "
                        "VALUES('steam:10','steam','10',10)"
                    )
                    await conn.execute(
                        "INSERT INTO local_game_state(user_id,game_id,state,updated_at) "
                        "VALUES(?,'steam:10','want_to_play','2026-10-01T00:00:00Z')", (user_id,)
                    )
                    await conn.execute(
                        "INSERT INTO price_tracking(user_id,game_id,added_at) "
                        "VALUES(?,'steam:10','2026-10-01T00:00:00Z')", (user_id,)
                    )
                    await conn.commit()
                await db.record_price_snapshots([
                    (10, "US", "USD", "priced", 1000, 800, 20, "2026-10-01T00:00:00Z", "test")
                ])
                await db.record_library_observation(
                    user_id, "2026-10-01", 1, 1, 60, "2026-10-01T00:00:00Z", [(10, 60)]
                )
                await db.record_library_observation(
                    user_id, "2026-10-02", 1, 1, 90, "2026-10-02T00:00:00Z", [(10, 90)]
                )

                async def fake_library(steam_id: str, force: bool = False) -> dict[str, object]:
                    return {
                        "game_count": 1,
                        "games": [{"appid": 10, "name": "Example", "playtime_forever": 90}],
                        "_service_fetched_at": "2026-10-02T00:00:00Z",
                    }

                app.state.steam.get_owned_games = fake_library
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="https://steam.example.com") as client:
                    response = await client.get("/export.json", cookies={"steam_mcp_web": token})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment", response.headers["content-disposition"])
        data = response.json()
        self.assertEqual(data["library"]["summary"]["total_games"], 1)
        self.assertEqual(data["library"]["items"][0]["name"], "Example")
        self.assertEqual(data["backlog"]["explicit_states"][0]["state"], "want_to_play")
        self.assertEqual(data["observed_price_history"]["10"][0]["final_price_minor"], 800)
        self.assertEqual(data["activity_history"][0]["delta_minutes"], 30)
        self.assertEqual(data["event_history"][0]["event_type"], "playtime_changed")
        self.assertNotIn("auth", data)
        self.assertNotIn("steam_id", json.dumps(data).casefold())
        self.assertNotIn("steam api key", json.dumps(data).casefold())


if __name__ == "__main__":
    unittest.main()
