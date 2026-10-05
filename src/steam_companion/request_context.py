from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    steam_id: str


@dataclass(frozen=True, slots=True)
class UserContext:
    user_id: str
    steam_id: str
    country_code: str
    preferred_language: str
    expected_currency: str


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    """Volatile wallet values fetched for one authenticated request only."""

    amount_minor: int | None
    currency: str | None
    updated_at: int | None
    source: str

    def as_settings(self) -> Mapping[str, object]:
        return MappingProxyType({
            "wallet_amount_minor": self.amount_minor,
            "wallet_currency": self.currency,
            "wallet_updated_at": self.updated_at,
            "wallet_source": self.source,
        })


@dataclass(slots=True)
class RequestLocalState:
    wallet: WalletSnapshot | None = None


@dataclass(frozen=True, slots=True)
class RequestContext:
    principal: Principal
    user_context: UserContext | None = None
    local: RequestLocalState = field(default_factory=RequestLocalState, compare=False)

    def with_user_context(self, user_context: UserContext) -> RequestContext:
        return replace(self, user_context=user_context)


current_request_context: ContextVar[RequestContext | None] = ContextVar(
    "steam_request_context", default=None
)
