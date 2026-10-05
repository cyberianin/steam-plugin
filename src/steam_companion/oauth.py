from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiosqlite
import httpx
import jwt
from cryptography.hazmat.primitives import serialization

from steam_companion.cimd import CimdResolver, OAuthClient
from steam_companion.config import Settings
from steam_companion.errors import OAuthError
from steam_companion.http_policy import steam_http_policy
from steam_companion.storage import Database


STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/"
STEAM_ID_CLAIM = re.compile(r"^https?://steamcommunity\.com/openid/id/([0-9]{17})$")
ACCESS_TOKEN_SECONDS = 20 * 60
CODE_SECONDS = 3 * 60
REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
MANAGEMENT_SESSION_SECONDS = 30 * 24 * 60 * 60
TX_SECONDS = 10 * 60
SCOPE = "steam:read"
COOKIE_NAME = "steam_mcp_browser"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_s256(value: str) -> str:
    return base64url(hashlib.sha256(value.encode("ascii")).digest())


def _now() -> int:
    return int(time.time())


class OAuthService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        http: httpx.AsyncClient,
        clients: CimdResolver,
    ) -> None:
        self.settings = settings
        self.db = db
        self.http = http
        self.clients = clients
        self.issuer = settings.origin
        self.resource = settings.origin
        self._private_key = settings.oauth_signing_key.get_secret_value()
        try:
            key = serialization.load_pem_private_key(self._private_key.encode(), password=None)
            if not hasattr(key, "public_key") or key.__class__.__name__ != "Ed25519PrivateKey":
                raise TypeError("not an Ed25519 private key")
            self._private_key = key
            self._public_key = key.public_key()
        except Exception as exc:
            raise ValueError("OAUTH_SIGNING_KEY must be an Ed25519 PEM private key") from exc

    def public_key_jwk(self) -> dict[str, str]:
        return json.loads(jwt.algorithms.Ed25519Algorithm.to_jwk(self._public_key))

    async def validate_authorize_request(
        self,
        *,
        response_type: str,
        client_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
        resource: str,
        scope: str,
    ) -> OAuthClient:
        if response_type != "code":
            raise OAuthError("unsupported_response_type", "Only the authorization-code flow is supported.")
        if not state or len(state) > 2048:
            raise OAuthError("invalid_request", "A valid OAuth state parameter is required.")
        if len(code_challenge) < 43 or len(code_challenge) > 128 or code_challenge_method != "S256":
            raise OAuthError("invalid_request", "PKCE S256 is required.")
        if resource != self.resource:
            raise OAuthError("invalid_target", "The requested resource does not match this MCP server.")
        if scope != SCOPE:
            raise OAuthError("invalid_scope", f"Only {SCOPE} is supported.")
        client = await self.clients.get(client_id)
        if redirect_uri not in client.redirect_uris:
            raise OAuthError("invalid_request", "The redirect URI is not registered for this client.")
        return client

    async def begin_authorization(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        browser_secret: str,
    ) -> str:
        transaction_id = secrets.token_urlsafe(32)
        async with self.db.connection() as db:
            await db.execute(
                "INSERT INTO oauth_transactions "
                "(id, client_id, redirect_uri, client_state, code_challenge, scope, resource, browser_hash, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transaction_id,
                    client_id,
                    redirect_uri,
                    state,
                    code_challenge,
                    SCOPE,
                    self.resource,
                    digest(browser_secret),
                    _now() + TX_SECONDS,
                ),
            )
            await db.commit()
        return transaction_id

    async def start_steam_login(self, transaction_id: str, browser_secret: str) -> str:
        async with self.db.connection() as db:
            cursor = await db.execute(
                "SELECT browser_hash, expires_at, consumed_at FROM oauth_transactions WHERE id=?",
                (transaction_id,),
            )
            tx = await cursor.fetchone()
            if (
                tx is None
                or tx["consumed_at"] is not None
                or tx["expires_at"] < _now()
                or not hmac.compare_digest(tx["browser_hash"], digest(browser_secret))
            ):
                raise OAuthError("invalid_request", "This login request expired. Start OAuth again.", 400)
            state = secrets.token_urlsafe(32)
            return_to = f"{self.settings.origin}/auth/steam/callback?state={state}"
            await db.execute(
                "INSERT INTO steam_openid_states(state_hash, transaction_id, browser_hash, return_to, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (digest(state), transaction_id, digest(browser_secret), return_to, _now() + 300),
            )
            await db.commit()
        params = {
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "openid.mode": "checkid_setup",
            "openid.return_to": return_to,
            "openid.realm": f"{self.settings.origin}/",
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        }
        return f"{STEAM_OPENID_ENDPOINT}?{urlencode(params)}"

    async def complete_steam_login(
        self,
        *,
        state: str,
        browser_secret: str,
        openid_fields: dict[str, str],
    ) -> tuple[str, str]:
        async with self.db.connection() as db:
            cursor = await db.execute(
                "SELECT * FROM steam_openid_states WHERE state_hash=?", (digest(state),)
            )
            row = await cursor.fetchone()
        if (
            row is None
            or row["consumed_at"] is not None
            or row["expires_at"] < _now()
            or not hmac.compare_digest(row["browser_hash"], digest(browser_secret))
        ):
            raise OAuthError("invalid_request", "Steam login state is invalid, expired, or already used.", 400)

        self._validate_openid_fields(openid_fields, row["return_to"])
        openid_host = steam_http_policy.validate_url(STEAM_OPENID_ENDPOINT)
        await steam_http_policy.acquire(openid_host)
        try:
            verification = await self.http.post(
                STEAM_OPENID_ENDPOINT,
                data={**openid_fields, "openid.mode": "check_authentication"},
                timeout=8,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise OAuthError("temporarily_unavailable", "Steam identity verification is unavailable.", 503) from exc
        if verification.status_code != 200 or "is_valid:true" not in verification.text.replace("\r", "").split("\n"):
            raise OAuthError("access_denied", "Steam could not verify the identity response.", 403)

        claimed_id = openid_fields.get("openid.claimed_id", "")
        match = STEAM_ID_CLAIM.fullmatch(claimed_id)
        steam_id = match.group(1) if match else ""
        if not steam_id or steam_id not in self.settings.allowed_steam_id_set:
            raise OAuthError("access_denied", "This Steam account is not allowed to use this service.", 403)

        if not self._fresh_openid_nonce(openid_fields.get("openid.response_nonce", "")):
            raise OAuthError("invalid_request", "Steam identity response is expired or malformed.", 400)
        return await self._finish_transaction(row, steam_id, openid_fields["openid.response_nonce"])

    @staticmethod
    def _validate_openid_fields(fields: dict[str, str], expected_return_to: str) -> None:
        if fields.get("openid.ns") != "http://specs.openid.net/auth/2.0":
            raise OAuthError("access_denied", "Steam returned an invalid OpenID namespace.", 403)
        if fields.get("openid.mode") != "id_res":
            raise OAuthError("access_denied", "Steam did not return an authenticated identity.", 403)
        if fields.get("openid.op_endpoint") != STEAM_OPENID_ENDPOINT:
            raise OAuthError("access_denied", "Steam returned an unexpected identity provider.", 403)
        if fields.get("openid.return_to") != expected_return_to:
            raise OAuthError("access_denied", "Steam returned to an unexpected URL.", 403)
        if not fields.get("openid.response_nonce"):
            raise OAuthError("access_denied", "Steam omitted its signed response nonce.", 403)
        signed = set(fields.get("openid.signed", "").split(","))
        required = {"op_endpoint", "claimed_id", "identity", "return_to", "response_nonce"}
        if not required.issubset(signed):
            raise OAuthError("access_denied", "Steam's signed identity fields are incomplete.", 403)
        identity = fields.get("openid.identity", "")
        claimed_id = fields.get("openid.claimed_id", "")
        if identity != claimed_id or not STEAM_ID_CLAIM.fullmatch(claimed_id):
            raise OAuthError("access_denied", "Steam returned a malformed Steam identity.", 403)

    @staticmethod
    def _fresh_openid_nonce(value: str) -> bool:
        # OpenID 2.0 nonce begins with an ISO-8601 UTC timestamp and ends with provider entropy.
        if len(value) < 20:
            return False
        try:
            timestamp = datetime.fromisoformat(value[:20].replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return abs(_now() - int(timestamp)) <= 600

    async def _finish_transaction(self, state_row: aiosqlite.Row, steam_id: str, nonce: str) -> tuple[str, str]:
        async with self.db.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE steam_openid_states SET consumed_at=? WHERE state_hash=? AND consumed_at IS NULL AND expires_at>=?",
                (_now(), state_row["state_hash"], _now()),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise OAuthError("invalid_request", "Steam login state is invalid or already used.", 400)
            cursor = await db.execute("SELECT * FROM oauth_transactions WHERE id=?", (state_row["transaction_id"],))
            tx = await cursor.fetchone()
            if (
                tx is None
                or tx["consumed_at"] is not None
                or tx["expires_at"] < _now()
                or not hmac.compare_digest(tx["browser_hash"], state_row["browser_hash"])
            ):
                await db.rollback()
                raise OAuthError("invalid_request", "This authorization request expired or was already used.", 400)
            try:
                await db.execute("INSERT INTO openid_nonces(nonce, created_at) VALUES (?, ?)", (nonce, _now()))
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise OAuthError("invalid_request", "Steam's identity response was already used.", 400) from exc
            user_id = str(uuid.uuid4())
            cursor = await db.execute("SELECT id FROM users WHERE steam_id=?", (steam_id,))
            existing = await cursor.fetchone()
            if existing:
                user_id = existing["id"]
            else:
                await db.execute("INSERT INTO users(id, steam_id, created_at) VALUES (?, ?, ?)", (user_id, steam_id, _now()))
                await db.execute(
                    "INSERT INTO user_settings(user_id, store_country_code, preferred_language, expected_currency) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, self.settings.store_country_default, self.settings.store_language_default, self.settings.store_currency_default),
                )
            await db.execute("UPDATE oauth_transactions SET user_id=? WHERE id=?", (user_id, tx["id"]))
            web_session = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            await db.execute(
                "INSERT INTO web_sessions(session_hash, user_id, transaction_id, csrf_hash, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (digest(web_session), user_id, tx["id"], digest(csrf_token), _now() + TX_SECONDS, _now()),
            )
            await db.commit()
        return web_session, csrf_token

    async def web_session_context(
        self, session_token: str, *, rotate_csrf: bool = True
    ) -> dict[str, object] | None:
        session_hash = digest(session_token)
        now = _now()
        csrf_token = secrets.token_urlsafe(32) if rotate_csrf else ""
        async with self.db.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT ms.user_id,ms.csrf_hash,ms.expires_at,us.store_country_code,us.preferred_language,"
                "us.expected_currency,us.wallet_amount_minor,us.wallet_currency,us.wallet_updated_at,u.steam_id "
                "FROM management_sessions ms JOIN user_settings us ON us.user_id=ms.user_id "
                "JOIN users u ON u.id=ms.user_id WHERE ms.session_hash=?",
                (session_hash,),
            )
            row = await cursor.fetchone()
            if row is not None:
                if row["expires_at"] < now or row["steam_id"] not in self.settings.allowed_steam_id_set:
                    await db.execute("DELETE FROM management_sessions WHERE session_hash=?", (session_hash,))
                    await db.commit()
                    return None
                if rotate_csrf:
                    await db.execute(
                        "UPDATE management_sessions SET csrf_hash=?,expires_at=? WHERE session_hash=?",
                        (digest(csrf_token), now + MANAGEMENT_SESSION_SECONDS, session_hash),
                    )
            await db.commit()
        is_management = row is not None
        if row is None:
            async with self.db.connection() as db:
                cursor = await db.execute(
                    "SELECT ws.*, us.store_country_code, us.preferred_language, us.expected_currency, "
                    "us.wallet_amount_minor, us.wallet_currency, us.wallet_updated_at,u.steam_id "
                    "FROM web_sessions ws JOIN user_settings us ON us.user_id=ws.user_id "
                    "JOIN users u ON u.id=ws.user_id WHERE ws.session_hash=?",
                    (session_hash,),
                )
                row = await cursor.fetchone()
            if row is None or row["expires_at"] < now or row["steam_id"] not in self.settings.allowed_steam_id_set:
                return None
            if rotate_csrf:
                async with self.db.connection() as db:
                    await db.execute(
                        "UPDATE web_sessions SET csrf_hash=? WHERE session_hash=?",
                        (digest(csrf_token), session_hash),
                    )
                    await db.commit()
        context = dict(row)
        context["csrf_token"] = csrf_token
        context["management_session"] = is_management
        return context

    async def save_settings_and_issue_code(
        self,
        *,
        session_token: str,
        csrf_token: str,
        country: str,
        language: str,
        currency: str,
        wallet_action: str,
        wallet_amount_minor: int | None,
    ) -> str:
        if not re.fullmatch(r"[A-Za-z]{2}", country) or not re.fullmatch(r"[A-Za-z]{3}", currency):
            raise OAuthError("invalid_request", "Country or currency code is invalid.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,20}", language):
            raise OAuthError("invalid_request", "Language code is invalid.")
        if wallet_action not in {"keep", "clear", "set"}:
            raise OAuthError("invalid_request", "Wallet action is invalid.")
        if wallet_action == "set" and (wallet_amount_minor is None or wallet_amount_minor < 0):
            raise OAuthError("invalid_request", "Enter a non-negative wallet balance in minor units.")
        async with self.db.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT ws.*, tx.* FROM web_sessions ws JOIN oauth_transactions tx "
                "ON tx.id=ws.transaction_id WHERE ws.session_hash=?",
                (digest(session_token),),
            )
            row = await cursor.fetchone()
            if (
                row is None
                or row["expires_at"] < _now()
                or row["consumed_at"] is not None
                or not hmac.compare_digest(row["csrf_hash"], digest(csrf_token))
            ):
                await db.rollback()
                raise OAuthError("invalid_request", "The settings form expired. Restart Steam login.", 400)
            wallet_sql = "wallet_amount_minor=wallet_amount_minor, wallet_currency=wallet_currency, wallet_updated_at=wallet_updated_at"
            wallet_args: tuple[object, ...] = ()
            if wallet_action == "clear":
                wallet_sql = "wallet_amount_minor=NULL, wallet_currency=NULL, wallet_updated_at=NULL"
            elif wallet_action == "set":
                wallet_sql = "wallet_amount_minor=?, wallet_currency=?, wallet_updated_at=?"
                wallet_args = (wallet_amount_minor, currency.upper(), _now())
            await db.execute(
                "UPDATE user_settings SET store_country_code=?, preferred_language=?, expected_currency=?, "
                + wallet_sql
                + " WHERE user_id=?",
                (country.upper(), language, currency.upper(), *wallet_args, row["user_id"]),
            )
            code = secrets.token_urlsafe(32)
            await db.execute(
                "INSERT INTO authorization_codes "
                "(code_hash, user_id, client_id, redirect_uri, code_challenge, scope, resource, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(code), row["user_id"], row["client_id"], row["redirect_uri"], row["code_challenge"],
                    row["scope"], row["resource"], _now() + CODE_SECONDS,
                ),
            )
            await db.execute("UPDATE oauth_transactions SET consumed_at=? WHERE id=?", (_now(), row["transaction_id"]))
            await db.execute(
                "INSERT INTO management_sessions(session_hash,user_id,csrf_hash,expires_at,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(session_hash) DO UPDATE SET user_id=excluded.user_id,"
                "csrf_hash=excluded.csrf_hash,expires_at=excluded.expires_at",
                (digest(session_token), row["user_id"], digest(secrets.token_urlsafe(32)), _now() + MANAGEMENT_SESSION_SECONDS, _now()),
            )
            await db.execute("DELETE FROM web_sessions WHERE session_hash=?", (digest(session_token),))
            await db.commit()
        return self._authorization_redirect(row["redirect_uri"], code, row["client_state"])

    async def save_backlog_entry(
        self,
        *,
        session_token: str,
        user_id: str,
        csrf_token: str,
        appid: int,
        state: str | None,
        note: str | None,
        user_score: float | None,
        priority: int | None,
        watched: bool,
        track_price: bool,
    ) -> str:
        next_csrf = secrets.token_urlsafe(32)
        await self.db.save_local_game(
            user_id,
            appid,
            state=state,
            note=note,
            user_score=user_score,
            priority=priority,
            watched=watched,
            track_price=track_price,
            updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            session_hash=digest(session_token),
            csrf_hash=digest(csrf_token),
            next_csrf_hash=digest(next_csrf),
            allowed_steam_ids=self.settings.allowed_steam_id_set,
        )
        return next_csrf

    async def save_management_settings(
        self,
        *,
        session_token: str,
        csrf_token: str,
        country: str,
        language: str,
        currency: str,
        wallet_action: str,
        wallet_amount_minor: int | None,
    ) -> str:
        if not re.fullmatch(r"[A-Za-z]{2}", country) or not re.fullmatch(r"[A-Za-z]{3}", currency):
            raise OAuthError("invalid_request", "Country or currency code is invalid.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,20}", language):
            raise OAuthError("invalid_request", "Language code is invalid.")
        if wallet_action not in {"keep", "clear", "set"}:
            raise OAuthError("invalid_request", "Wallet action is invalid.")
        if wallet_action == "set" and (wallet_amount_minor is None or wallet_amount_minor < 0):
            raise OAuthError("invalid_request", "Enter a non-negative wallet balance in minor units.")
        session_hash = digest(session_token)
        next_csrf = secrets.token_urlsafe(32)
        async with self.db.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT ms.user_id,ms.csrf_hash,ms.expires_at,u.steam_id FROM management_sessions ms "
                "JOIN users u ON u.id=ms.user_id WHERE ms.session_hash=?",
                (session_hash,),
            )
            row = await cursor.fetchone()
            if (
                row is None or row["expires_at"] < _now()
                or row["steam_id"] not in self.settings.allowed_steam_id_set
                or not hmac.compare_digest(str(row["csrf_hash"]), digest(csrf_token))
            ):
                await db.rollback()
                raise OAuthError("invalid_request", "The settings form expired. Reload the page and try again.", 400)
            wallet_sql = "wallet_amount_minor=wallet_amount_minor,wallet_currency=wallet_currency,wallet_updated_at=wallet_updated_at"
            wallet_args: tuple[object, ...] = ()
            if wallet_action == "clear":
                wallet_sql = "wallet_amount_minor=NULL,wallet_currency=NULL,wallet_updated_at=NULL"
            elif wallet_action == "set":
                wallet_sql = "wallet_amount_minor=?,wallet_currency=?,wallet_updated_at=?"
                wallet_args = (wallet_amount_minor, currency.upper(), _now())
            await db.execute(
                "UPDATE user_settings SET store_country_code=?,preferred_language=?,expected_currency=?,"
                + wallet_sql + " WHERE user_id=?",
                (country.upper(), language, currency.upper(), *wallet_args, row["user_id"]),
            )
            await db.execute(
                "UPDATE management_sessions SET csrf_hash=?,expires_at=? WHERE session_hash=?",
                (digest(next_csrf), _now() + MANAGEMENT_SESSION_SECONDS, session_hash),
            )
            await db.commit()
        return next_csrf

    def _authorization_redirect(self, redirect_uri: str, code: str, state: str) -> str:
        return self._authorization_response(redirect_uri, state, code=code)

    def _authorization_response(
        self, redirect_uri: str, state: str, *, code: str | None = None, error: str | None = None
    ) -> str:
        parts = urlsplit(redirect_uri)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if code is not None:
            query.append(("code", code))
        if error is not None:
            query.append(("error", error))
        query.extend([("state", state), ("iss", self.issuer)])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def authorization_error_redirect(
        self, *, state: str, browser_secret: str, error: str
    ) -> str | None:
        async with self.db.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT s.state_hash, s.transaction_id, s.consumed_at AS state_consumed_at, s.expires_at AS state_expires_at, "
                "t.redirect_uri, t.client_state, t.consumed_at AS transaction_consumed_at "
                "FROM steam_openid_states s JOIN oauth_transactions t ON t.id=s.transaction_id "
                "WHERE s.state_hash=? AND s.browser_hash=?",
                (digest(state), digest(browser_secret)),
            )
            row = await cursor.fetchone()
            if (
                row is None
                or row["state_consumed_at"] is not None
                or row["state_expires_at"] < _now()
                or row["transaction_consumed_at"] is not None
            ):
                await db.rollback()
                return None
            await db.execute(
                "UPDATE steam_openid_states SET consumed_at=? WHERE state_hash=? AND consumed_at IS NULL",
                (_now(), row["state_hash"]),
            )
            await db.execute(
                "UPDATE oauth_transactions SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (_now(), row["transaction_id"]),
            )
            await db.commit()
        return self._authorization_response(row["redirect_uri"], row["client_state"], error=error)

    async def exchange_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code: str,
        verifier: str,
        resource: str,
    ) -> dict[str, object]:
        await self.clients.get(client_id)
        if not (43 <= len(verifier) <= 128) or resource != self.resource:
            raise OAuthError("invalid_grant", "The authorization grant is invalid.")
        async with self.db.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT ac.*,u.steam_id FROM authorization_codes ac JOIN users u ON u.id=ac.user_id "
                "WHERE ac.code_hash=?",
                (digest(code),),
            )
            row = await cursor.fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or row["expires_at"] < _now()
                or row["client_id"] != client_id
                or row["redirect_uri"] != redirect_uri
                or row["resource"] != resource
                or not hmac.compare_digest(row["code_challenge"], pkce_s256(verifier))
            ):
                await db.rollback()
                raise OAuthError("invalid_grant", "The authorization code is invalid, expired, or already used.")
            await db.execute("UPDATE authorization_codes SET consumed_at=? WHERE code_hash=?", (_now(), digest(code)))
            refresh = secrets.token_urlsafe(48)
            await db.execute(
                "INSERT INTO refresh_tokens(token_hash, family_id, user_id, client_id, scope, resource, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (digest(refresh), str(uuid.uuid4()), row["user_id"], client_id, row["scope"], resource, _now() + REFRESH_TOKEN_SECONDS, _now()),
            )
            await db.commit()
        return self._token_response(row["user_id"], row["steam_id"], row["scope"], resource, refresh)

    async def rotate_refresh_token(self, *, client_id: str, refresh_token: str, resource: str) -> dict[str, object]:
        await self.clients.get(client_id)
        if resource != self.resource:
            raise OAuthError("invalid_target", "The requested resource does not match this server.")
        old_hash = digest(refresh_token)
        async with self.db.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT rt.*,u.steam_id FROM refresh_tokens rt JOIN users u ON u.id=rt.user_id WHERE rt.token_hash=?",
                (old_hash,),
            )
            row = await cursor.fetchone()
            if row is None or row["expires_at"] < _now() or row["resource"] != resource or row["client_id"] != client_id:
                await db.rollback()
                raise OAuthError("invalid_grant", "The refresh token is invalid or expired.")
            if row["revoked_at"] is not None:
                await db.execute("UPDATE refresh_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE family_id=?", (_now(), row["family_id"]))
                await db.commit()
                raise OAuthError("invalid_grant", "This refresh token was already used.")
            replacement = secrets.token_urlsafe(48)
            replacement_hash = digest(replacement)
            await db.execute("UPDATE refresh_tokens SET revoked_at=?, replaced_by=? WHERE token_hash=?", (_now(), replacement_hash, old_hash))
            await db.execute(
                "INSERT INTO refresh_tokens(token_hash, family_id, user_id, client_id, scope, resource, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (replacement_hash, row["family_id"], row["user_id"], row["client_id"], row["scope"], resource, _now() + REFRESH_TOKEN_SECONDS, _now()),
            )
            await db.commit()
        return self._token_response(row["user_id"], row["steam_id"], row["scope"], resource, replacement)

    def _token_response(
        self, user_id: str, steam_id: str, scope: str, resource: str, refresh: str
    ) -> dict[str, object]:
        now = _now()
        claims = {
            "iss": self.issuer,
            "sub": user_id,
            "steam_id": steam_id,
            "aud": resource,
            "iat": now,
            "exp": now + ACCESS_TOKEN_SECONDS,
            "scope": scope,
            "jti": str(uuid.uuid4()),
        }
        access = jwt.encode(claims, self._private_key, algorithm="EdDSA", headers={"kid": "steam-companion-1"})
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_SECONDS,
            "refresh_token": refresh,
            "scope": scope,
        }

    def validate_access_token(self, token: str) -> dict[str, object]:
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["EdDSA"],
                issuer=self.issuer,
                audience=self.resource,
                options={"require": ["iss", "sub", "aud", "iat", "exp", "scope", "jti", "steam_id"]},
            )
        except jwt.PyJWTError as exc:
            raise OAuthError("invalid_token", "The access token is invalid or expired.", 401) from exc
        if claims.get("scope") != SCOPE:
            raise OAuthError("insufficient_scope", "The access token does not grant steam:read.", 403)
        steam_id = claims.get("steam_id")
        if not isinstance(steam_id, str) or not re.fullmatch(r"[0-9]{17}", steam_id):
            raise OAuthError("invalid_token", "The access token Steam identity is invalid.", 401)
        if steam_id not in self.settings.allowed_steam_id_set:
            raise OAuthError("account_not_allowed", "This Steam account is not allowed.", 403)
        return claims

    async def get_user_steam_id(self, user_id: str) -> str:
        async with self.db.connection() as db:
            cursor = await db.execute("SELECT steam_id FROM users WHERE id=?", (user_id,))
            row = await cursor.fetchone()
        if row is None:
            raise OAuthError("invalid_token", "The access token user is no longer available.", 401)
        return row["steam_id"]

    async def get_user_settings(self, user_id: str) -> dict[str, object]:
        async with self.db.connection() as db:
            cursor = await db.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,))
            row = await cursor.fetchone()
        if row is None:
            raise OAuthError("invalid_token", "The account settings are unavailable.", 401)
        return dict(row)
