from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from steam_companion.library import LibrarySnapshot
from steam_companion.models import PackageAnalysis, PackageItem
from steam_companion.request_context import UserContext
from steam_companion.steam import utc_now_iso

PackageLoader = Callable[[int, str, str], Awaitable[dict[str, Any]]]
LibraryLoader = Callable[..., Awaitable[tuple[LibrarySnapshot, int, str]]]


class PackageAnalysisService:
    """Joins an unofficial Storefront package response with one complete library snapshot."""

    MAX_PACKAGE_APPS = 500

    def __init__(self, load_package: PackageLoader, load_library: LibraryLoader):
        self._load_package = load_package
        self._load_library = load_library

    async def analyze(self, package_id: int, user_context: UserContext) -> PackageAnalysis:
        raw_result, library_result = await asyncio.gather(
            self._load_package(package_id, user_context.country_code, user_context.preferred_language),
            self._load_library(steam_id=user_context.steam_id),
            return_exceptions=True,
        )
        if isinstance(raw_result, BaseException):
            raise raw_result

        warnings = ["Package details use Steam's unofficial Storefront interface."]
        if isinstance(library_result, BaseException):
            owned_appids: set[int] = set()
            warnings.append("Ownership could not be checked because the library was unavailable.")
        else:
            library, _, _ = library_result
            owned_appids = set(library.by_appid)

        raw_apps = raw_result.get("apps")
        apps: list[PackageItem] = []
        seen: set[int] = set()
        if isinstance(raw_apps, list):
            for raw in raw_apps:
                if not isinstance(raw, dict):
                    continue
                appid = raw.get("id")
                if isinstance(appid, bool) or not isinstance(appid, int) or appid <= 0 or appid in seen:
                    continue
                seen.add(appid)
                if len(apps) >= self.MAX_PACKAGE_APPS:
                    warnings.append("Package contents were truncated at the supported AppID limit.")
                    break
                apps.append(PackageItem(
                    appid=appid,
                    name=raw.get("name") if isinstance(raw.get("name"), str) else None,
                    # Absence from GetOwnedGames is ambiguous (free-to-play, hidden,
                    # or otherwise omitted); report only positive ownership evidence.
                    owned=True if appid in owned_appids else None,
                    ownership="owned" if appid in owned_appids else "unknown",
                ))
        else:
            warnings.append("Package contents were missing or malformed; ownership overlap is incomplete.")

        price = raw_result.get("price")
        price = price if isinstance(price, dict) else {}
        initial = self._integer(price.get("initial"))
        final = self._integer(price.get("final"))
        currency = price.get("currency") if isinstance(price.get("currency"), str) else None
        discount = self._integer(raw_result.get("discount_percent"))
        if discount is None and initial is not None and final is not None and initial > 0:
            discount = max(0, min(100, round((initial - final) * 100 / initial)))
        owned = [item.appid for item in apps if item.owned is True]
        unknown_count = sum(item.ownership == "unknown" for item in apps)
        known_not_owned_count = sum(item.ownership == "not_owned" for item in apps)
        if unknown_count:
            warnings.append(
                "Items absent from the visible owned-games response remain unknown; free-to-play and hidden titles may be omitted."
            )
        fully_covered = (
            None if not apps or (unknown_count and not known_not_owned_count)
            else all(item.ownership == "owned" for item in apps)
        )
        return PackageAnalysis(
            package_id=package_id,
            name=raw_result.get("name") if isinstance(raw_result.get("name"), str) else None,
            price_state="priced" if final is not None else "missing_price",
            currency=currency,
            initial_price_minor=initial,
            final_price_minor=final,
            discount_percent=discount,
            apps=apps,
            owned_appids=owned,
            duplicate_owned_count=len(owned),
            included_app_count=len(apps),
            known_owned_count=len(owned),
            unknown_item_count=unknown_count,
            confirmed_coverage_percent=round(len(owned) * 100 / len(apps), 1) if apps else None,
            fully_covered=fully_covered,
            source="steam_storefront_packagedetails",
            source_stability="unofficial",
            fetched_at=utc_now_iso(),
            warnings=warnings,
        )

    @staticmethod
    def _integer(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
