from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from steam_companion.errors import OAuthError


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    redirect_uris: tuple[str, ...]
    token_auth_methods: tuple[str, ...]


class CimdResolver:
    """Fetches only OpenAI-hosted CIMD documents to avoid arbitrary URL/SSRF access."""

    ALLOWED_HOSTS = {"chatgpt.com", "www.chatgpt.com"}
    CACHE_MAX_ENTRIES = 64

    def __init__(self, http: httpx.AsyncClient, ttl_seconds: int = 900) -> None:
        self.http = http
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[float, OAuthClient]] = OrderedDict()
        self._lock = asyncio.Lock()

    @classmethod
    def validate_document_url(cls, client_id: str) -> None:
        parsed = urlsplit(client_id)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in cls.ALLOWED_HOSTS
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise OAuthError("invalid_client", "The client metadata URL is not allowed.", 401)
        if parsed.path != "/oauth/client.json" and not re.fullmatch(
            r"/oauth/[A-Za-z0-9_-]{1,128}/client\.json", parsed.path
        ):
            raise OAuthError("invalid_client", "The client metadata path is not supported.", 401)

    async def get(self, client_id: str) -> OAuthClient:
        self.validate_document_url(client_id)
        now = time.monotonic()
        cached = self._cache.get(client_id)
        if cached and cached[0] > now:
            self._cache.move_to_end(client_id)
            return cached[1]
        async with self._lock:
            cached = self._cache.get(client_id)
            if cached and cached[0] > time.monotonic():
                self._cache.move_to_end(client_id)
                return cached[1]
            try:
                response = await self.http.get(client_id, follow_redirects=False, timeout=5.0)
            except httpx.HTTPError as exc:
                raise OAuthError("invalid_client", "Could not fetch the client metadata document.", 401) from exc
            if response.status_code != 200 or len(response.content) > 65536:
                raise OAuthError("invalid_client", "The client metadata document is unavailable.", 401)
            try:
                data = response.json()
            except ValueError as exc:
                raise OAuthError("invalid_client", "The client metadata is not valid JSON.", 401) from exc
            if not isinstance(data, dict) or data.get("client_id") != client_id:
                raise OAuthError("invalid_client", "The metadata does not identify this client URL.", 401)
            redirects = data.get("redirect_uris")
            if not isinstance(redirects, list) or not redirects or not all(
                isinstance(uri, str) and self._valid_redirect(uri) for uri in redirects
            ):
                raise OAuthError("invalid_client", "The client metadata has invalid redirect URIs.", 401)
            methods = data.get("token_endpoint_auth_methods_supported")
            if methods is None:
                legacy = data.get("token_endpoint_auth_method")
                methods = [legacy] if isinstance(legacy, str) else []
            if not isinstance(methods, list) or "none" not in methods:
                raise OAuthError("unauthorized_client", "This server supports only public OAuth clients.", 401)
            client = OAuthClient(client_id, tuple(redirects), tuple(methods))
            self._cache[client_id] = (time.monotonic() + self.ttl_seconds, client)
            self._cache.move_to_end(client_id)
            while len(self._cache) > self.CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)
            return client

    @staticmethod
    def _valid_redirect(uri: str) -> bool:
        parsed = urlsplit(uri)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.username is None
            and parsed.password is None
            and parsed.fragment == ""
        )
