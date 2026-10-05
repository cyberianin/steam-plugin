from __future__ import annotations

from datetime import UTC, datetime
from typing import Mapping, Protocol

from steam_companion.config import Settings
from steam_companion.models import WalletState
from steam_companion.money import format_minor_units


class WalletProvider(Protocol):
    async def get_state(self, user_settings: Mapping[str, object]) -> WalletState: ...


class LocalWalletProvider:
    """Wallet adapter for the explicitly user-entered, non-live balance."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def get_state(self, user_settings: Mapping[str, object]) -> WalletState:
        amount = user_settings["wallet_amount_minor"]
        currency = user_settings["wallet_currency"]
        updated = user_settings["wallet_updated_at"]
        age = max(0, int(datetime.now(UTC).timestamp()) - int(updated)) if updated else None
        return WalletState(
            available=amount is not None and currency is not None,
            amount_minor=amount,
            currency=currency,
            formatted=format_minor_units(int(amount), str(currency)) if amount is not None and currency else None,
            updated_at=datetime.fromtimestamp(updated, UTC).isoformat().replace("+00:00", "Z") if updated else None,
            age_seconds=age,
            source=str(user_settings["wallet_source"]),
            live=False,
            stale=age is not None and age > self.settings.wallet_stale_after_seconds,
        )
