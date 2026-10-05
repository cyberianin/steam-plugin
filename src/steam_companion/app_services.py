from __future__ import annotations

from dataclasses import dataclass

from steam_companion.backlog import BacklogService
from steam_companion.comparison import GameComparisonService
from steam_companion.coop import CoopService
from steam_companion.game_context import GameContextService
from steam_companion.library_service import LibraryService
from steam_companion.achievement_service import AchievementService
from steam_companion.wishlist_service import WishlistService
from steam_companion.price_history import PriceHistoryService
from steam_companion.observations import ObservationService
from steam_companion.package_analysis import PackageAnalysisService
from steam_companion.purchase_context import PurchaseContextService
from steam_companion.account_service import AccountService
from steam_companion.review_service import ReviewService
from steam_companion.capabilities_service import CapabilitiesService
from steam_companion.storefront_service import StorefrontService


@dataclass(frozen=True, slots=True)
class AppServices:
    account: AccountService
    reviews: ReviewService
    capabilities: CapabilitiesService
    observations: ObservationService
    backlog: BacklogService
    game_context: GameContextService
    comparison: GameComparisonService
    coop: CoopService
    package_analysis: PackageAnalysisService
    purchase_context: PurchaseContextService
    library: LibraryService
    achievements: AchievementService
    wishlist: WishlistService
    price_history: PriceHistoryService
    storefront: StorefrontService
