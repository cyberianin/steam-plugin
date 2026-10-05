from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    public_base_url: str
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, ge=1, le=65535)
    steam_web_api_key: SecretStr
    allowed_steam_ids: str
    store_country_default: str = "KZ"
    store_language_default: str = "english"
    store_currency_default: str = "KZT"
    database_url: str = "sqlite+aiosqlite:////data/steam-companion.sqlite3"
    oauth_signing_key: SecretStr
    session_secret: SecretStr
    enable_unofficial_storefront: bool = False
    mcp_toolset: str = "core"
    library_cache_ttl_seconds: int = Field(default=300, ge=1, le=86400)
    store_cache_ttl_seconds: int = Field(default=300, ge=1, le=86400)
    wallet_stale_after_seconds: int = Field(default=86400, ge=60, le=31536000)
    log_level: str = "INFO"
    steam_timeout_seconds: float = Field(default=9.0, gt=0, le=30)
    steam_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=10)

    @field_validator("public_base_url")
    @classmethod
    def canonical_public_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin URL")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("PUBLIC_BASE_URL must not contain a path, query, or fragment")
        return value.rstrip("/")

    @field_validator("allowed_steam_ids")
    @classmethod
    def valid_allowlist(cls, value: str) -> str:
        ids = [item.strip() for item in value.split(",") if item.strip()]
        if len(ids) != 1 or any(not item.isdigit() or len(item) != 17 for item in ids):
            raise ValueError("ALLOWED_STEAM_IDS must contain exactly one 17-digit SteamID64 in this release")
        if len(set(ids)) != len(ids):
            raise ValueError("ALLOWED_STEAM_IDS contains duplicates")
        return ",".join(ids)

    @field_validator("store_country_default")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("STORE_COUNTRY_DEFAULT must be a two-letter country code")
        return value

    @field_validator("mcp_toolset")
    @classmethod
    def valid_toolset(cls, value: str) -> str:
        if value not in {"core", "full"}:
            raise ValueError("MCP_TOOLSET must be core or full")
        return value

    @model_validator(mode="after")
    def key_material_is_present(self) -> Settings:
        if not self.steam_web_api_key.get_secret_value().strip():
            raise ValueError("STEAM_WEB_API_KEY is required")
        if not self.oauth_signing_key.get_secret_value().strip():
            raise ValueError("OAUTH_SIGNING_KEY is required")
        if len(self.session_secret.get_secret_value().encode()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 bytes")
        if not self.database_url.startswith("sqlite+aiosqlite:///"):
            raise ValueError("DATABASE_URL must use the async SQLite driver in this release")
        return self

    @property
    def allowed_steam_id_set(self) -> frozenset[str]:
        return frozenset(self.allowed_steam_ids.split(","))

    @property
    def origin(self) -> str:
        return self.public_base_url

    @property
    def host(self) -> str:
        return urlsplit(self.public_base_url).hostname or ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
