from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import httpx

from steam_companion.errors import ServiceError
from steam_companion.models import GameMetadata, Provenance, StoreOffer
from steam_companion.http_policy import steam_http_policy
from steam_companion.steam import utc_now_iso


@dataclass(frozen=True, slots=True)
class AppDetailsSnapshot:
    appid: int
    country_code: str
    language: str
    fetched_at: str
    data: Mapping[str, Any] | None
    error: str | None = None


class StoreProvider:
    """Optional public Steam Storefront adapter; isolated from domain contracts."""

    ORIGIN = "https://store.steampowered.com"
    CURRENCY_EXPONENT = {
        "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3,
        "CLP": 0, "ISK": 0, "JPY": 0, "KRW": 0, "KZT": 0, "VND": 0,
    }
    OFFER_CACHE_MAX_ENTRIES = 256
    APPDETAILS_CACHE_MAX_ENTRIES = 128
    REVIEW_CACHE_MAX_ENTRIES = 128

    def __init__(self, http: httpx.AsyncClient, *, enabled: bool, ttl: int = 300, max_concurrency: int = 6):
        self.http = http
        self.enabled = enabled
        self.ttl = ttl
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cache: OrderedDict[tuple[int, str, str, str], tuple[float, StoreOffer]] = OrderedDict()
        self._inflight: dict[tuple[int, str, str, str], asyncio.Task[StoreOffer]] = {}
        self._appdetails_cache: OrderedDict[
            tuple[int, str, str], tuple[float, AppDetailsSnapshot]
        ] = OrderedDict()
        self._appdetails_inflight: dict[tuple[int, str, str], asyncio.Task[AppDetailsSnapshot]] = {}
        self._cache_lock = asyncio.Lock()
        self._review_cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
        self._review_locks: dict[tuple[int, str], asyncio.Lock] = {}

    async def get_offer(
        self, appid: int, country_code: str, language: str, expected_currency: str, *, fresh: bool = False
    ) -> StoreOffer:
        key = (appid, country_code.upper(), language, expected_currency.upper())
        async with self._cache_lock:
            task = self._inflight.get(key)
            if task is None:
                cached = self._cache.get(key)
                if not fresh and cached and cached[0] > time.monotonic():
                    self._cache.move_to_end(key)
                    return cached[1]
                if fresh:
                    self._appdetails_cache.pop((appid, country_code.upper(), language), None)
                task = asyncio.create_task(
                    self._fetch(appid, country_code.upper(), language, expected_currency.upper())
                )
                self._inflight[key] = task
                task.add_done_callback(
                    lambda completed: asyncio.create_task(self._finish_offer(key, completed))
                )
        try:
            offer = await asyncio.shield(task)
        except BaseException:
            if task.done():
                await self._finish_offer(key, task)
            raise
        await self._finish_offer(key, task)
        return offer

    async def _finish_offer(self, key: tuple[int, str, str, str], task: asyncio.Task[StoreOffer]) -> None:
        async with self._cache_lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
                try:
                    offer = task.result()
                except BaseException:
                    return
                self._cache[key] = (time.monotonic() + self.ttl, offer)
                self._cache.move_to_end(key)
                while len(self._cache) > self.OFFER_CACHE_MAX_ENTRIES:
                    self._cache.popitem(last=False)

    async def get_offers(
        self, appids: list[int], country_code: str, language: str, expected_currency: str, *, fresh: bool = False
    ) -> list[StoreOffer]:
        if len(appids) > 50:
            raise ValueError("A store offer batch may contain at most 50 AppIDs")

        return await asyncio.gather(
            *(self.get_offer(appid, country_code, language, expected_currency, fresh=fresh) for appid in appids)
        )

    async def _appdetails(
        self, appid: int, country_code: str, language: str, *, fresh: bool = False
    ) -> AppDetailsSnapshot:
        key = (appid, country_code.upper(), language)
        async with self._cache_lock:
            task = self._appdetails_inflight.get(key)
            if task is None:
                cached = self._appdetails_cache.get(key)
                if not fresh and cached and cached[0] > time.monotonic():
                    self._appdetails_cache.move_to_end(key)
                    return cached[1]
                task = asyncio.create_task(self._load_appdetails(appid, key[1], language))
                self._appdetails_inflight[key] = task
                task.add_done_callback(
                    lambda completed: asyncio.create_task(self._finish_appdetails(key, completed))
                )
        try:
            snapshot = await asyncio.shield(task)
        except BaseException:
            if task.done():
                await self._finish_appdetails(key, task)
            raise
        await self._finish_appdetails(key, task)
        return snapshot

    async def _finish_appdetails(
        self, key: tuple[int, str, str], task: asyncio.Task[AppDetailsSnapshot]
    ) -> None:
        async with self._cache_lock:
            if self._appdetails_inflight.get(key) is task:
                self._appdetails_inflight.pop(key, None)
                try:
                    snapshot = task.result()
                except BaseException:
                    return
                self._appdetails_cache[key] = (time.monotonic() + self.ttl, snapshot)
                self._appdetails_cache.move_to_end(key)
                while len(self._appdetails_cache) > self.APPDETAILS_CACHE_MAX_ENTRIES:
                    self._appdetails_cache.popitem(last=False)

    async def _load_appdetails(self, appid: int, country_code: str, language: str) -> AppDetailsSnapshot:
        fetched_at = utc_now_iso()
        if not self.enabled:
            return AppDetailsSnapshot(appid, country_code, language, fetched_at, None, "store_provider_disabled")
        url = f"{self.ORIGIN}/api/appdetails"
        host = steam_http_policy.validate_url(url)
        try:
            async with self._semaphore:
                await steam_http_policy.acquire(host)
                response = await self.http.get(
                    url,
                    params={"appids": appid, "cc": country_code.lower(), "l": language, "json": 1},
                    timeout=8.0,
                    follow_redirects=False,
                )
        except httpx.TimeoutException:
            return AppDetailsSnapshot(appid, country_code, language, fetched_at, None, "store_timeout")
        except httpx.RequestError:
            return AppDetailsSnapshot(appid, country_code, language, fetched_at, None, "store_unavailable")
        if response.status_code == 429:
            return AppDetailsSnapshot(appid, country_code, language, fetched_at, None, "upstream_rate_limited")
        if response.status_code != 200:
            return AppDetailsSnapshot(appid, country_code, language, fetched_at, None, "store_unavailable")
        try:
            body = response.json()
            envelope = body.get(str(appid)) if isinstance(body, dict) else None
            if not isinstance(envelope, dict):
                raise ValueError
            if envelope.get("success") is not True or not isinstance(envelope.get("data"), dict):
                return AppDetailsSnapshot(appid, country_code, language, fetched_at, None)
            data = MappingProxyType(envelope["data"])
        except (ValueError, TypeError, AttributeError):
            return AppDetailsSnapshot(appid, country_code, language, fetched_at, None, "store_malformed_response")
        return AppDetailsSnapshot(appid, country_code, language, fetched_at, data)

    async def get_package_details(self, package_id: int, country_code: str, language: str) -> dict[str, Any]:
        if package_id <= 0:
            raise ServiceError("invalid_package_id", "Package ID must be positive.", 400)
        if not self.enabled:
            raise ServiceError("store_provider_unavailable", "Steam package details are disabled.", 503)
        url = f"{self.ORIGIN}/api/packagedetails"
        host = steam_http_policy.validate_url(url)
        try:
            async with self._semaphore:
                await steam_http_policy.acquire(host)
                response = await self.http.get(
                    url,
                    params={"packageids": package_id, "cc": country_code.lower(), "l": language},
                    timeout=httpx.Timeout(8.0, connect=3.0),
                    follow_redirects=False,
                )
        except httpx.TimeoutException:
            raise ServiceError("package_timeout", "Steam package details timed out.", 504) from None
        except httpx.RequestError:
            raise ServiceError("package_unavailable", "Steam package details are unavailable.", 503) from None
        if response.status_code == 429:
            raise ServiceError("upstream_rate_limited", "Steam rate limited the package request.", 503)
        if response.status_code != 200:
            raise ServiceError("package_unavailable", "Steam package details are unavailable.", 503)
        try:
            body = response.json()
            envelope = body.get(str(package_id)) if isinstance(body, dict) else None
            if (
                not isinstance(envelope, dict)
                or envelope.get("success") is not True
                or not isinstance(envelope.get("data"), dict)
            ):
                raise ValueError
            return dict(envelope["data"])
        except (ValueError, TypeError, AttributeError):
            raise ServiceError("package_malformed_response", "Steam returned malformed package details.", 502) from None

    async def get_metadata(
        self, appid: int, country_code: str, language: str, *, fresh: bool = False
    ) -> GameMetadata:
        snapshot = await self._appdetails(appid, country_code, language, fresh=fresh)
        data = snapshot.data
        if data is None:
            code = snapshot.error or "store_game_unavailable"
            raise ServiceError(code, "Steam Store metadata is unavailable for this game.", 503)

        def strings(key: str, child: str | None = None, limit: int = 100) -> list[str]:
            values = data.get(key)
            if not isinstance(values, list):
                return []
            result: list[str] = []
            for value in values[:limit]:
                candidate = value.get(child) if isinstance(value, dict) and child else value
                if isinstance(candidate, str) and candidate.strip():
                    result.append(candidate.strip()[:160])
            return result

        release = data.get("release_date")
        release_date = release.get("date") if isinstance(release, dict) else None
        categories = strings("categories", "description")
        category_text = " ".join(categories).casefold()
        platforms_raw = data.get("platforms")
        platforms = (
            [key for key, active in platforms_raw.items() if active is True]
            if isinstance(platforms_raw, dict) else []
        )
        language_text = data.get("supported_languages")
        languages = (
            [part.strip()[:80] for part in re.sub(r"<[^>]*>", "", language_text).split(",") if part.strip()][:100]
            if isinstance(language_text, str) else []
        )
        deck = data.get("steam_deck_compatibility")
        deck_category = deck.get("category") if isinstance(deck, dict) else None
        deck_labels = {0: "unknown", 1: "unsupported", 2: "playable", 3: "verified"}
        metacritic = data.get("metacritic")
        score = metacritic.get("score") if isinstance(metacritic, dict) else None
        return GameMetadata(
            appid=appid,
            name=data.get("name") if isinstance(data.get("name"), str) else None,
            game_type=data.get("type") if isinstance(data.get("type"), str) else None,
            release_date=release_date[:80] if isinstance(release_date, str) else None,
            coming_soon=release.get("coming_soon") if isinstance(release, dict) and isinstance(release.get("coming_soon"), bool) else None,
            developers=strings("developers"),
            publishers=strings("publishers"),
            genres=strings("genres", "description"),
            categories=categories,
            platforms=platforms[:10],
            controller_support=data.get("controller_support") if isinstance(data.get("controller_support"), str) else None,
            coop=("co-op" in category_text or "coop" in category_text) if categories else None,
            multiplayer=("multi-player" in category_text or "multiplayer" in category_text) if categories else None,
            steam_deck=deck_labels.get(deck_category) if isinstance(deck_category, int) else None,
            languages=languages,
            dlc_appids=[value for value in data.get("dlc", [])[:200] if isinstance(value, int) and value > 0]
            if isinstance(data.get("dlc"), list) else [],
            package_ids=[value for value in data.get("packages", [])[:100] if isinstance(value, int) and value > 0]
            if isinstance(data.get("packages"), list) else [],
            metacritic_score=score if isinstance(score, int) and 0 <= score <= 100 else None,
            provenance={
                "source": "Steam Storefront appdetails",
                "provider": "steam_storefront_appdetails",
                "fetched_at": snapshot.fetched_at,
                "complete": True,
                "truncated": False,
                "stale": False,
                "warnings": ["The public Storefront appdetails interface is unofficial."],
            },
        )

    async def get_review_summary(self, appid: int, review_filter: str = "all") -> dict[str, Any]:
        if review_filter not in {"all", "recent"}:
            raise ValueError("Unsupported review filter")
        key = (appid, review_filter)
        async with self._cache_lock:
            cached = self._review_cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            lock = self._review_locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                async with self._cache_lock:
                    cached = self._review_cache.get(key)
                    if cached and cached[0] > time.monotonic():
                        return cached[1]
                result = await self._load_review_summary(appid, review_filter)
                async with self._cache_lock:
                    self._review_cache[key] = (time.monotonic() + self.ttl, result)
                    while len(self._review_cache) > self.REVIEW_CACHE_MAX_ENTRIES:
                        self._review_cache.pop(next(iter(self._review_cache)))
                return result
            finally:
                async with self._cache_lock:
                    if self._review_locks.get(key) is lock:
                        self._review_locks.pop(key, None)

    async def _load_review_summary(self, appid: int, review_filter: str) -> dict[str, Any]:
        url = f"{self.ORIGIN}/appreviews/{appid}"
        host = steam_http_policy.validate_url(url)
        try:
            await steam_http_policy.acquire(host)
            response = await self.http.get(
                url,
                params={"json": 1, "filter": review_filter, "language": "all", "num_per_page": 1},
                timeout=httpx.Timeout(8.0, connect=3.0),
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise ServiceError("reviews_timeout", "Steam reviews did not respond before the timeout.", 504) from None
        except httpx.RequestError:
            raise ServiceError("reviews_unavailable", "Steam reviews are temporarily unavailable.", 503) from None
        if response.status_code == 429:
            raise ServiceError("upstream_rate_limited", "Steam rate limited the request.", 503)
        if response.status_code != 200:
            raise ServiceError("reviews_unavailable", "Steam returned an unexpected reviews response.", 502)
        try:
            body = response.json()
            summary = body.get("query_summary")
            if body.get("success") != 1 or not isinstance(summary, dict):
                raise ValueError
            normalized = {
                "review_score": summary.get("review_score") if isinstance(summary.get("review_score"), int) else None,
                "review_score_description": summary.get("review_score_desc") if isinstance(summary.get("review_score_desc"), str) else None,
                "total_reviews": summary.get("total_reviews") if isinstance(summary.get("total_reviews"), int) else None,
                "positive_reviews": summary.get("total_positive") if isinstance(summary.get("total_positive"), int) else None,
                "negative_reviews": summary.get("total_negative") if isinstance(summary.get("total_negative"), int) else None,
            }
            if any(normalized[name] is None for name in ("total_reviews", "positive_reviews", "negative_reviews")):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ServiceError("reviews_malformed_response", "Steam returned incomplete reviews data.", 502) from None
        normalized["fetched_at"] = utc_now_iso()
        normalized["source"] = "steam_appreviews"
        return normalized

    async def _fetch(self, appid: int, country: str, language: str, expected_currency: str) -> StoreOffer:
        base = {
            "appid": appid,
            "country_code": country,
            "currency": None,
            "base_price_minor": None,
            "final_price_minor": None,
            "discount_percent": None,
            "price_state": "provider_error",
            "currency_mismatch": False,
            "store_url": f"https://store.steampowered.com/app/{appid}/",
            "fetched_at": utc_now_iso(),
            "source": "steam_storefront_appdetails",
            "source_stability": "unofficial",
        }
        if not self.enabled:
            return StoreOffer(**base, error="store_provider_disabled")
        snapshot = await self._appdetails(appid, country, language)
        base["fetched_at"] = snapshot.fetched_at
        if snapshot.data is None:
            if snapshot.error:
                return StoreOffer(**base, error=snapshot.error)
            base["price_state"] = "not_available_in_region"
            return StoreOffer(**base)
        data = snapshot.data
        base["name"] = data.get("name") if isinstance(data.get("name"), str) else None
        price = data.get("price_overview")
        if data.get("is_free") is True:
            base.update(currency=expected_currency, base_price_minor=0, final_price_minor=0, discount_percent=0, price_state="free")
        elif isinstance(price, dict) and isinstance(price.get("currency"), str):
            currency = price["currency"].upper()
            initial = self._integer(price.get("initial"))
            final = self._integer(price.get("final"))
            discount = self._integer(price.get("discount_percent"))
            if initial is None or final is None:
                base["price_state"] = "missing_price"
            else:
                # Steam's Storefront price_overview reports integer minor currency units.
                base.update(
                    currency=currency,
                    base_price_minor=initial,
                    final_price_minor=final,
                    discount_percent=discount,
                    price_state="priced",
                    currency_mismatch=currency != expected_currency,
                )
        elif isinstance(data.get("release_date"), dict) and data["release_date"].get("coming_soon") is True:
            base["price_state"] = "unreleased"
        elif data.get("is_free") is False:
            base["price_state"] = "not_for_sale"
        else:
            base["price_state"] = "missing_price"
        return StoreOffer(**base)

    @staticmethod
    def _integer(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        return None
