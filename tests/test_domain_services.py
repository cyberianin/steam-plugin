from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from steam_companion.backlog import BacklogService
from steam_companion.account_service import AccountService
from steam_companion.capabilities_service import CapabilitiesService
from steam_companion.comparison import GameComparisonService
from steam_companion.coop import CoopService
from steam_companion.errors import ServiceError
from steam_companion.game_context import GameContextService
from steam_companion.library import LibrarySnapshot
from steam_companion.library_service import LibraryService
from steam_companion.models import GameRecord, StoreOffer
from steam_companion.pagination import SnapshotCursor
from steam_companion.request_context import UserContext
from steam_companion.models import WishlistItem
from steam_companion.wishlist_service import WishlistService
from steam_companion.models import WalletState
from steam_companion.purchase_context import PurchaseContextService
from steam_companion.review_service import ReviewService
from steam_companion.storage import Database
from steam_companion.storefront_service import StorefrontService


def library(*games: GameRecord) -> LibrarySnapshot:
    return LibrarySnapshot.build(list(games), "snapshot-1", len(games))


def offer(appid: int, state: str = "priced") -> StoreOffer:
    return StoreOffer(
        appid=appid, name=f"Game {appid}", country_code="US", currency="USD",
        final_price_minor=1000 if state == "priced" else None, price_state=state,
        store_url=f"https://store.steampowered.com/app/{appid}/", fetched_at="now",
        source="test", source_stability="unofficial",
    )


USER = UserContext("user", "76561198000000000", "US", "english", "USD")


class DomainServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_library_read_observation_is_an_explicit_choice(self) -> None:
        calls = {"read": 0, "observe": 0}
        snapshot = library(GameRecord(appid=1, name="Game", playtime_forever_minutes=5))

        async def read(_steam: str, _fresh: bool):
            calls["read"] += 1
            return snapshot, 1, snapshot.fetched_at

        async def observe(_steam: str, _fresh: bool):
            calls["observe"] += 1
            return snapshot, 1, snapshot.fetched_at

        service = LibraryService(read, observe, b"cursor-secret" * 3, None, lambda *_a, **_kw: {})
        await service.summary(USER.steam_id)
        self.assertEqual(calls, {"read": 1, "observe": 0})
        await service.summary(USER.steam_id, observe=True)
        self.assertEqual(calls, {"read": 1, "observe": 1})

    async def test_storefront_separates_read_from_explicit_observation(self) -> None:
        calls = {"user": 0, "tracking": 0, "observations": 0, "store": 0}

        class FakeStore:
            async def get_offers(self, appids, country, language, currency, *, fresh=False):
                calls["store"] += 1
                self.assertions = (appids, country, language, currency, fresh)
                return [offer(appid) for appid in appids]

        class FakeDatabase:
            async def get_price_tracking(self, user_id):
                calls["tracking"] += 1
                self.assertions = user_id
                return [{"appid": 7}]

        class FakeObservations:
            async def observe_store_offers(self, offers, tracked_appids):
                calls["observations"] += 1
                self.assertions = ({item.appid for item in offers}, tracked_appids)

        async def load_user():
            calls["user"] += 1
            return USER

        service = StorefrontService(FakeStore(), FakeDatabase(), FakeObservations(), lambda: "user", load_user)
        offers, context = await service.offers([7])
        self.assertEqual(len(offers), 1)
        self.assertIs(context, USER)
        self.assertEqual(calls, {"user": 1, "tracking": 0, "observations": 0, "store": 1})

        await service.observe_offers([7], user_context=USER)
        self.assertEqual(calls, {"user": 1, "tracking": 1, "observations": 1, "store": 2})

        with self.assertRaises(ServiceError) as raised:
            await service.offers([7, 7], user_context=USER)
        self.assertEqual(raised.exception.code, "invalid_appid")

    async def test_account_service_owns_partial_profile_orchestration(self) -> None:
        calls = {"profile": 0, "level": 0}

        async def profile(_steam: str):
            calls["profile"] += 1
            raise RuntimeError("profile unavailable")

        async def level(_steam: str):
            calls["level"] += 1
            return 7

        account = await AccountService(profile, level).account(USER)
        self.assertEqual(calls, {"profile": 1, "level": 1})
        self.assertIsNone(account.persona_name)
        self.assertEqual(account.steam_level, 7)
        self.assertFalse(account.provenance.complete)
        self.assertTrue(account.provenance.warnings)

    async def test_review_service_normalizes_aggregate_and_capabilities_are_composed(self) -> None:
        calls = 0

        async def summary(appid: int, review_filter: str):
            nonlocal calls
            calls += 1
            self.assertEqual((appid, review_filter), (42, "recent"))
            return {"positive_reviews": 8, "negative_reviews": 2, "total_reviews": 10,
                    "review_score": 8, "review_score_description": "Very Positive",
                    "fetched_at": "snapshot"}

        result = await ReviewService(summary).summary(42, "recent")
        self.assertEqual(calls, 1)
        self.assertEqual(result.positive_percent, 80.0)
        self.assertEqual(result.total_reviews, 10)
        self.assertIn("review text is not returned", result.provenance.warnings[0])
        core = CapabilitiesService(full_mode=False, store_enabled=False).build()
        self.assertFalse(core.activity_history["available"])
        self.assertFalse(core.store_prices["available"])
        self.assertFalse(core.local_sidecar["available"])

    async def test_backlog_is_read_only_and_achievement_scan_is_bounded_and_partial(self) -> None:
        games = [GameRecord(appid=i, name=f"Game {i}", playtime_minutes=1500,
                            playtime_forever_minutes=1500) for i in range(1, 9)]
        snapshot = library(*games)
        active = 0
        peak = 0

        async def achievements(_steam: str, appid: int, _language: str) -> dict:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            if appid == 2:
                raise RuntimeError("one app failed")
            names = ["a", "b", "c", "d", "e"]
            return {"definitions": [{"name": name} for name in names],
                    "player_achievements": [{"apiname": name, "achieved": True} for name in names[:4]]}

        async def rows(_user: str) -> list[dict]:
            return []

        async def load_library(_steam: str):
            return snapshot, len(snapshot), snapshot.fetched_at

        service = BacklogService(rows, rows, rows, load_library, achievements)
        result = await service.build(USER, achievement_scan_limit=8)
        self.assertEqual(peak, 3)
        self.assertEqual(len(result["inferred"]["near_100_percent"]), 7)
        self.assertEqual(snapshot.items, tuple(games))
        self.assertNotIn("state", result["inferred"]["classification_basis"])

    async def test_comparison_uses_one_indexed_library_and_one_offer_batch(self) -> None:
        calls = {"library": 0, "offers": 0}
        snapshot = library(GameRecord(appid=1, name="Owned"))

        async def load_library(_steam: str):
            calls["library"] += 1
            return snapshot, 1, "snapshot-1"

        async def load_offers(appids: list[int], _user: UserContext):
            calls["offers"] += 1
            return [offer(appid) for appid in appids]

        result = await GameComparisonService(load_library, load_offers).compare([1, 2, 3], USER)
        self.assertEqual(calls, {"library": 1, "offers": 1})
        self.assertEqual([item.ownership for item in result.games], ["owned", "not_owned", "not_owned"])

    async def test_game_context_preserves_partial_result_and_unknown_ownership(self) -> None:
        calls = {"library": 0, "offer": 0, "metadata": 0}

        async def load_library(_steam: str, _fresh: bool):
            calls["library"] += 1
            raise RuntimeError("private library")

        async def load_offer(_appid: int, _user: UserContext, _fresh: bool):
            calls["offer"] += 1
            return offer(42)

        async def load_metadata(_appid: int, _user: UserContext, _fresh: bool):
            calls["metadata"] += 1
            raise RuntimeError("metadata unavailable")

        async def achievements(*_args):
            return {"definitions": [], "player_achievements": []}

        async def reviews(_appid: int, _kind: str):
            return {"positive_reviews": 1, "negative_reviews": 0, "total_reviews": 1}

        async def players(_appid: int):
            return {"player_count": 10}

        service = GameContextService(load_library, load_offer, load_metadata, achievements, reviews, players)
        result = await service.build(42, USER)
        self.assertEqual(calls, {"library": 1, "offer": 1, "metadata": 1})
        self.assertEqual(result.ownership, "unknown")
        self.assertEqual(result.current_players, 10)
        self.assertIsNotNone(result.lifetime_reviews)
        self.assertTrue(result.provenance.warnings)

    async def test_coop_limits_friend_library_concurrency_and_excludes_private(self) -> None:
        current = 0
        peak = 0

        async def friends(_steam: str):
            return [{"steamid": str(i), "friend_since": i} for i in range(1, 9)]

        async def owner(_steam: str):
            return library(GameRecord(appid=1, name="Owned")), 1, "snapshot"

        async def friend_library(friend_id: str):
            nonlocal current, peak
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.005)
            current -= 1
            if friend_id == "8":
                raise ServiceError("friend_list_private", "private", 403)
            return {"games": [{"appid": 1}]}

        async def summaries(ids: list[str]):
            return [{"steamid": item, "personaname": f"Friend {item}"} for item in ids]

        result = await CoopService(friends, friend_library, summaries, owner).build("owner", [1], 8)
        self.assertLessEqual(peak, 3)
        self.assertEqual(result.friends_with_public_libraries, 7)
        self.assertEqual(len(result.matches), 7)
        self.assertTrue(result.provenance.warnings)
        self.assertNotIn("steamid", result.matches[0].model_dump())

    async def test_snapshot_cursor_distinguishes_stale_and_invalid_binding(self) -> None:
        secret = b"test secret"
        cursor = SnapshotCursor.encode(12, "principal-a", {"sort": "name"}, secret, "gen-1", "library")
        self.assertEqual(SnapshotCursor.decode(cursor, "principal-a", {"sort": "name"}, secret, "gen-1", "library"), 12)
        with self.assertRaises(ServiceError) as stale:
            SnapshotCursor.decode(cursor, "principal-a", {"sort": "name"}, secret, "gen-2", "library")
        self.assertEqual(stale.exception.code, "cursor_stale")
        with self.assertRaises(ServiceError) as invalid:
            SnapshotCursor.decode(cursor, "principal-b", {"sort": "name"}, secret, "gen-1", "library")
        self.assertEqual(invalid.exception.code, "invalid_cursor")

    async def test_wishlist_pages_are_bound_to_the_same_snapshot(self) -> None:
        async def load(_steam: str):
            return {"items": [], "_service_fetched_at": "unused"}

        async def observe(_user: str, _appids: list[int], _fetched_at: str):
            return None

        service = WishlistService(load, observe, b"secret")
        items = [WishlistItem(appid=i) for i in range(1, 4)]
        first = service.page("user", items, "generation-1", None, 1)
        self.assertIsNotNone(first.next_cursor)
        second = service.page("user", items, "generation-1", first.next_cursor, 1)
        self.assertEqual(second.items[0].appid, 2)
        with self.assertRaises(ServiceError) as stale:
            service.page("user", items, "generation-2", first.next_cursor, 1)
        self.assertEqual(stale.exception.code, "cursor_stale")

    async def test_wishlist_observed_page_is_one_explicit_application_operation(self) -> None:
        calls = {"load": 0, "observe": 0}

        async def load(_steam: str):
            calls["load"] += 1
            return {"items": [{"appid": 1}, {"appid": 2}], "_service_fetched_at": "generation-1"}

        async def observe(_user: str, _appids: list[int], _fetched_at: str):
            calls["observe"] += 1

        service = WishlistService(load, observe, b"secret")
        first = await service.observe_page("user", "steam", None, 1)
        second = await service.observe_page("user", "steam", first.next_cursor, 1)
        self.assertEqual(second.items[0].appid, 2)
        self.assertEqual(calls, {"load": 2, "observe": 2})

    async def test_bounded_library_comparison_purchase_and_observation_stress(self) -> None:
        games = [GameRecord(appid=i, name=f"Game {i:04}", playtime_minutes=i,
                            playtime_forever_minutes=i) for i in range(1, 2501)]
        snapshot = library(*games)

        class Wallet:
            async def get_state(self, _settings):
                return WalletState(available=False, amount_minor=None, currency=None, formatted=None,
                                   updated_at=None, age_seconds=None, source="manual", live=False, stale=False)

        with tempfile.TemporaryDirectory() as temp:
            db = Database(f"sqlite+aiosqlite:///{Path(temp) / 'stress.sqlite3'}")
            await db.initialize()
            async with db.connection() as conn:
                await conn.execute("INSERT INTO users(id,steam_id,created_at) VALUES('stress-user',?,1)",
                                   (USER.steam_id,))
                await conn.commit()

            observation_calls = 0

            async def observe_library(steam_id: str, fresh: bool = False):
                self.assertEqual(steam_id, USER.steam_id)
                nonlocal observation_calls
                observation_calls += 1
                await db.record_library_observation(
                    "stress-user", "2026-10-02", len(snapshot), len(snapshot),
                    sum(game.playtime_minutes or 0 for game in snapshot), "2026-10-02T10:00:00Z",
                    [(game.appid, game.playtime_minutes or 0) for game in snapshot],
                )
                return snapshot, len(snapshot), snapshot.fetched_at

            async def load_offers(appids: list[int], user_context: UserContext = USER,
                                  tracked_appids: set[int] | None = None):
                return [offer(appid) for appid in appids], user_context

            async def compare_offers(appids: list[int], user: UserContext):
                offers, _ = await load_offers(appids, user_context=user, tracked_appids=set())
                return offers

            async def load_wishlist(_steam_id: str):
                return [], "2026-10-02T10:00:00Z"

            comparison = GameComparisonService(observe_library, compare_offers)
            purchase = PurchaseContextService(db, Wallet(), observe_library, load_offers, load_wishlist)
            library_service = LibraryService(observe_library, observe_library, b"x" * 32, db,
                                             lambda *_args, **_kwargs: asyncio.sleep(0, result={"games": []}))
            comparison_result, purchase_result, page, _ = await asyncio.gather(
                comparison.compare([1, 2, 3, 4, 5], USER),
                purchase.build(USER, [1, 2, 3, 4, 5], wallet_settings={}),
                library_service.page("stress-user", USER.steam_id, query=None, played="any", sort="name",
                                     limit=50, cursor=None),
                observe_library(USER.steam_id),
            )
            events = await db.get_event_journal("stress-user", 20)
            async with db.connection() as conn:
                cursor = await conn.execute("SELECT COUNT(*) FROM library_presence WHERE user_id='stress-user'")
                observed_rows = (await cursor.fetchone())[0]
        self.assertEqual(comparison_result.returned, 5)
        self.assertEqual(len(purchase_result.items), 5)
        self.assertEqual(page.total_matching, 2500)
        self.assertEqual(observation_calls, 4)
        self.assertEqual(observed_rows, 2500)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
