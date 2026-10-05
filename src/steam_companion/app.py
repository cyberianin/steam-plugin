from __future__ import annotations

import contextlib
import asyncio
import html
import json
import logging
import re
import secrets
import time
import uuid
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from steam_companion.cimd import CimdResolver
from steam_companion.config import Settings, get_settings
from steam_companion.errors import OAuthError, ServiceError
from steam_companion.library import game_from_api
from steam_companion.mcp_server import create_mcp_server
from steam_companion.request_context import Principal, RequestContext, current_request_context
from steam_companion.oauth import COOKIE_NAME, MANAGEMENT_SESSION_SECONDS, OAuthService
from steam_companion.observability import emit, request_id
from steam_companion.storage import Database
from steam_companion.steam import SteamClient
from steam_companion.store import StoreProvider


logger = logging.getLogger("steam_companion")


class ProtectedMCP:
    def __init__(self, app: ASGIApp, auth: OAuthService, settings: Settings):
        self.app = app
        self.auth = auth
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        if not authorization.startswith("Bearer "):
            await self._unauthorized(send)
            return
        token = authorization[7:].strip()
        try:
            claims = self.auth.validate_access_token(token)
            user_id = str(claims["sub"])
            steam_id = str(claims["steam_id"])
        except OAuthError as exc:
            status = 403 if exc.status_code == 403 else 401
            await self._unauthorized(send, status=status, code=exc.code)
            return
        context_reset = current_request_context.set(RequestContext(Principal(user_id, steam_id)))
        try:
            await self.app(scope, receive, send)
        finally:
            current_request_context.reset(context_reset)

    async def _unauthorized(self, send: Send, status: int = 401, code: str = "invalid_token") -> None:
        metadata = f'{self.settings.origin}/.well-known/oauth-protected-resource'
        challenge = f'Bearer resource_metadata="{metadata}", scope="steam:read", error="{code}"'
        body = json.dumps({"error": code}).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"www-authenticate", challenge.encode("ascii")),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _set_private_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _form(body: bytes) -> dict[str, str]:
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError):
        raise OAuthError("invalid_request", "The form body is invalid.") from None
    return {key: values[0] for key, values in parsed.items() if values}


def _form_page(transaction_id: str) -> str:
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Connect Steam</title><style>body{font:16px system-ui;max-width:38rem;margin:4rem auto;padding:0 1rem;"
        "color:#e8edf2;background:#101820}a,button{background:#1b2838;color:white;padding:.8rem 1rem;border:0;"
        "border-radius:.4rem;font-size:1rem;text-decoration:none}p{line-height:1.5}</style><h1>Connect your Steam account</h1>"
        "<p>Steam will confirm your account on steamcommunity.com. This service never receives your Steam password, "
        "Steam Guard code, or Steam login cookies.</p>"
        f"<a href='/auth/steam?transaction={html.escape(transaction_id, quote=True)}'>Sign in through Steam</a></html>"
    )


def _settings_page(context: dict[str, Any], csrf: str, error: str | None = None) -> str:
    country = html.escape(str(context["store_country_code"]), quote=True)
    language = html.escape(str(context["preferred_language"]), quote=True)
    currency = html.escape(str(context["expected_currency"]), quote=True)
    wallet = context.get("wallet_amount_minor")
    amount = "" if wallet is None else str(wallet)
    message = f"<p role='alert'>{html.escape(error)}</p>" if error else ""
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Steam settings</title><style>body{font:16px system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem;"
        "color:#e8edf2;background:#101820}label{display:block;margin:1rem 0}input{display:block;padding:.5rem;margin-top:.3rem}"
        "button{background:#1b7a50;color:white;padding:.7rem 1rem;border:0;border-radius:.4rem}</style>"
        "<nav><a href='/backlog'>Backlog and watchlist</a></nav><h1>Store and wallet settings</h1><p>Set the country and currency used for public Steam offers. "
        "Wallet balance is optional and is always labeled as manual, never live.</p>"
        + message
        + "<form method='post' action='/settings'>"
        + f"<input type='hidden' name='csrf_token' value='{html.escape(csrf, quote=True)}'>"
        + f"<label>Store country <input name='country' value='{country}' pattern='[A-Za-z]{{2}}' required></label>"
        + f"<label>Preferred language <input name='language' value='{language}' required></label>"
        + f"<label>Expected currency <input name='currency' value='{currency}' pattern='[A-Za-z]{{3}}' required></label>"
        + f"<label>Manual wallet amount, integer minor units <input name='wallet_amount_minor' value='{amount}' inputmode='numeric'></label>"
        + "<label><input type='radio' name='wallet_action' value='keep' checked> Keep saved wallet</label>"
        + "<label><input type='radio' name='wallet_action' value='set'> Save the amount above in the selected currency</label>"
        + "<label><input type='radio' name='wallet_action' value='clear'> Clear saved wallet</label>"
        + "<button type='submit'>Save and continue</button></form></html>"
    )


def _backlog_page(
    csrf: str,
    entries: list[dict[str, Any]],
    watchlist: set[int],
    tracked: set[int],
    error: str | None = None,
) -> str:
    states = ["", "want_to_play", "playing", "paused", "finished", "dropped", "completed_100"]
    rows = []
    by_appid = {int(entry["appid"]): entry for entry in entries if entry.get("appid") is not None}
    for appid in sorted(set(by_appid) | watchlist | tracked):
        entry = by_appid.get(appid, {})
        selected_state = str(entry.get("state") or "")
        options = "".join(
            f"<option value='{state}'{' selected' if state == selected_state else ''}>{state or 'No explicit state'}</option>"
            for state in states
        )
        note = html.escape(str(entry.get("note") or ""), quote=True)
        score = "" if entry.get("user_score") is None else str(entry["user_score"])
        priority = "" if entry.get("priority") is None else str(entry["priority"])
        rows.append(
            "<form method='post' action='/backlog' class='entry'>"
            f"<input type='hidden' name='csrf_token' value='{html.escape(csrf, quote=True)}'>"
            f"<label>Steam AppID <input name='appid' type='number' min='1' max='2147483647' value='{appid}' required></label>"
            f"<label>State <select name='state'>{options}</select></label>"
            f"<label>Note <input name='note' maxlength='500' value='{note}'></label>"
            f"<label>Score 0–10 <input name='user_score' type='number' min='0' max='10' step='0.1' value='{score}'></label>"
            f"<label>Priority 0–100 <input name='priority' type='number' min='0' max='100' value='{priority}'></label>"
            f"<label><input type='checkbox' name='watched' value='yes'{' checked' if appid in watchlist else ''}> Watch this game</label>"
            f"<label><input type='checkbox' name='track_price' value='yes'{' checked' if appid in tracked else ''}> Track price history</label>"
            "<button>Save</button></form>"
        )
    rows.append(
        "<form method='post' action='/backlog' class='entry'>"
        f"<input type='hidden' name='csrf_token' value='{html.escape(csrf, quote=True)}'>"
        "<label>Steam AppID <input name='appid' type='number' min='1' max='2147483647' required></label>"
        "<label>State <select name='state'>"
        + "".join(f"<option value='{state}'>{state or 'No explicit state'}</option>" for state in states)
        + "</select></label><label>Note <input name='note' maxlength='500'></label>"
        "<label>Score 0–10 <input name='user_score' type='number' min='0' max='10' step='0.1'></label>"
        "<label>Priority 0–100 <input name='priority' type='number' min='0' max='100'></label>"
        "<label><input type='checkbox' name='watched' value='yes'> Watch this game</label>"
        "<label><input type='checkbox' name='track_price' value='yes'> Track price history</label><button>Save</button></form>"
    )
    message = f"<p role='alert'>{html.escape(error)}</p>" if error else ""
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Backlog and watchlist</title><style>body{font:16px system-ui;max-width:50rem;margin:3rem auto;padding:0 1rem;"
        "color:#e8edf2;background:#101820}a{color:#9bc}label{display:block;margin:.6rem 0}input,select{padding:.4rem}"
        ".entry{border:1px solid #456;border-radius:.5rem;padding:1rem;margin:1rem 0}button{padding:.6rem 1rem}</style>"
        "<nav><a href='/settings'>Store and wallet settings</a> · <a href='/export.json'>Download personal data</a></nav><h1>Backlog and watchlist</h1>"
        "<p>These are your local choices. The watchlist and price tracking are separate from Steam Wishlist and from each other. Price history can be tracked for up to 100 games and retains the latest 365 observations per region. Blank state removes the explicit backlog state.</p>"
        + message + "".join(rows) + "</html>"
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    # httpx logs full URLs at INFO; Steam authenticates these calls with a key in
    # the query string, so keep those request lines out of service logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db = Database(config.database_url)
    client_timeout = httpx.Timeout(config.steam_timeout_seconds, connect=config.steam_connect_timeout_seconds)
    upstream_http = httpx.AsyncClient(timeout=client_timeout, follow_redirects=False)
    cimd_http = httpx.AsyncClient(timeout=httpx.Timeout(5), follow_redirects=False)
    clients = CimdResolver(cimd_http)
    auth = OAuthService(config, db, cimd_http, clients)
    steam = SteamClient(
        upstream_http,
        config.steam_web_api_key.get_secret_value(),
        config.library_cache_ttl_seconds,
        config.steam_timeout_seconds,
        config.steam_connect_timeout_seconds,
    )
    store = StoreProvider(
        upstream_http,
        enabled=config.enable_unofficial_storefront,
        ttl=config.store_cache_ttl_seconds,
    )
    mcp = create_mcp_server(config, db, auth, steam, store)
    mcp_http = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=[config.host, f"{config.host}:443", f"{config.host}:*"],
            allowed_origins=[config.origin],
        )
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        await db.initialize()
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await upstream_http.aclose()
                await cimd_http.aclose()

    app = FastAPI(title="Steam Companion", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = config
    app.state.database = db
    app.state.oauth = auth
    app.state.steam = steam
    app.state.store = store
    app.state.mcp = mcp

    @app.middleware("http")
    async def request_logging(request: Request, call_next: Any) -> Response:
        supplied = request.headers.get("x-request-id", "")
        identifier = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else uuid.uuid4().hex
        marker = request_id.set(identifier)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            emit(
                logging.INFO,
                route=request.url.path,
                tool="mcp" if request.url.path == "/mcp" else None,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                status_class=f"{response.status_code // 100}xx",
                cache_hit=None,
            )
            response.headers["X-Request-ID"] = identifier
            return response
        finally:
            request_id.reset(marker)

    @app.get("/.well-known/oauth-protected-resource")
    async def protected_resource_metadata() -> dict[str, object]:
        return {
            "resource": config.origin,
            "authorization_servers": [config.origin],
            "scopes_supported": ["steam:read"],
            "bearer_methods_supported": ["header"],
        }

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata() -> dict[str, object]:
        return {
            "issuer": config.origin,
            "authorization_response_iss_parameter_supported": True,
            "authorization_endpoint": f"{config.origin}/oauth/authorize",
            "token_endpoint": f"{config.origin}/oauth/token",
            "jwks_uri": f"{config.origin}/.well-known/jwks.json",
            "client_id_metadata_document_supported": True,
            "token_endpoint_auth_methods_supported": ["none"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["steam:read"],
        }

    @app.get("/.well-known/jwks.json")
    async def jwks() -> dict[str, object]:
        jwk = auth.public_key_jwk()
        jwk.update(kid="steam-companion-1", use="sig", alg="EdDSA")
        return {"keys": [jwk]}

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readiness() -> JSONResponse:
        try:
            return JSONResponse({"status": "ready", "database": await db.readiness()})
        except Exception:
            return JSONResponse({"status": "not_ready", "database": False}, status_code=503)

    @app.get("/export.json")
    async def export_personal_data(request: Request) -> Response:
        token = request.cookies.get("steam_mcp_web")
        if not token:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        context = await auth.web_session_context(token)
        if context is None:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)

        user_id = str(context["user_id"])
        steam_id = str(context["steam_id"])
        library_response = await steam.get_owned_games(steam_id)
        fetched_at = str(library_response.get("_service_fetched_at", "unknown"))
        games = [game_from_api(item).model_dump(mode="json") for item in library_response["games"]]
        backlog, watchlist, tracking, activity, events = await asyncio.gather(
            db.get_local_backlog(user_id),
            db.get_local_watchlist(user_id),
            db.get_price_tracking(user_id),
            db.get_activity_deltas(user_id, None),
            db.get_event_journal(user_id, None),
        )
        tracked_appids = sorted({int(row["appid"]) for row in tracking if row.get("appid") is not None})
        price_history = await db.get_tracked_price_snapshots(tracked_appids)
        played = sum((item["playtime_forever_minutes"] or 0) > 0 for item in games)
        total_playtime = sum(item["playtime_forever_minutes"] or 0 for item in games)
        exported_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        document = {
            "schema_version": 1,
            "exported_at": exported_at,
            "library": {
                "summary": {
                    "total_games": int(library_response["game_count"]),
                    "played_games": played,
                    "total_playtime_minutes": total_playtime,
                    "data_fetched_at": fetched_at,
                },
                "items": games,
            },
            "backlog": {
                "explicit_states": backlog,
                "watchlist": watchlist,
                "price_tracking": tracking,
            },
            "observed_price_history": {
                str(appid): price_history.get(appid, []) for appid in tracked_appids
            },
            "activity_history": activity,
            "event_history": events,
            "provenance": {
                "source": "Steam Web API and local Steam Companion observations",
                "complete": True,
                "warnings": [
                    "Price history begins when this service first observed a tracked game's offer.",
                    "Activity history begins with the first complete library baseline.",
                ],
            },
        }
        response = JSONResponse(
            document,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="steam-companion-export.json"',
            },
        )
        _set_private_cookie(response, "steam_mcp_web", token, MANAGEMENT_SESSION_SECONDS)
        return response

    @app.get("/oauth/authorize", response_class=HTMLResponse)
    async def authorize(
        response_type: str,
        client_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
        resource: str,
        scope: str = "steam:read",
    ) -> Response:
        try:
            await auth.validate_authorize_request(
                response_type=response_type,
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                resource=resource,
                scope=scope,
            )
            browser_secret = secrets.token_urlsafe(32)
            txid = await auth.begin_authorization(
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
                browser_secret=browser_secret,
            )
            response = HTMLResponse(_form_page(txid))
            _set_private_cookie(response, COOKIE_NAME, browser_secret, 600)
            return response
        except OAuthError as exc:
            return JSONResponse({"error": exc.code, "error_description": exc.description}, status_code=exc.status_code)

    @app.get("/auth/steam")
    async def steam_login(transaction: str, request: Request) -> Response:
        browser_secret = request.cookies.get(COOKIE_NAME)
        if not browser_secret:
            return JSONResponse({"error": "login_session_missing"}, status_code=400)
        try:
            target = await auth.start_steam_login(transaction, browser_secret)
            return RedirectResponse(target, status_code=303)
        except OAuthError as exc:
            return JSONResponse({"error": exc.code, "error_description": exc.description}, status_code=exc.status_code)

    @app.get("/auth/steam/callback")
    async def steam_callback(request: Request) -> Response:
        browser_secret = request.cookies.get(COOKIE_NAME)
        state = request.query_params.get("state", "")
        if not browser_secret or not state:
            return JSONResponse({"error": "login_state_invalid"}, status_code=400)
        fields = {key: value for key, value in request.query_params.items() if key.startswith("openid.")}
        try:
            session_token, csrf_token = await auth.complete_steam_login(
                state=state,
                browser_secret=browser_secret,
                openid_fields=fields,
            )
            context = await auth.web_session_context(session_token)
            assert context is not None
            # web_session_context rotates the CSRF token and returns it in this context.
            response = HTMLResponse(_settings_page(context, str(context["csrf_token"])))
            _set_private_cookie(response, "steam_mcp_web", session_token, 600)
            response.delete_cookie(COOKIE_NAME, path="/")
            return response
        except OAuthError as exc:
            target = await auth.authorization_error_redirect(
                state=state,
                browser_secret=browser_secret,
                error=exc.code,
            )
            if target:
                response = RedirectResponse(target, status_code=303)
                response.delete_cookie(COOKIE_NAME, path="/")
                return response
            return JSONResponse({"error": exc.code, "error_description": exc.description}, status_code=exc.status_code)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        token = request.cookies.get("steam_mcp_web")
        if not token:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        context = await auth.web_session_context(token)
        if context is None:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        return HTMLResponse(_settings_page(context, str(context["csrf_token"])))

    @app.post("/settings")
    async def save_settings(request: Request) -> Response:
        token = request.cookies.get("steam_mcp_web")
        if not token:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("application/x-www-form-urlencoded"):
            return JSONResponse({"error": "invalid_request"}, status_code=415)
        session_context = await auth.web_session_context(token, rotate_csrf=False)
        if session_context is None:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        try:
            data = _form(await request.body())
            amount_raw = data.get("wallet_amount_minor", "").strip()
            amount = int(amount_raw) if amount_raw else None
            if session_context.get("management_session"):
                await auth.save_management_settings(
                    session_token=token,
                    csrf_token=data.get("csrf_token", ""),
                    country=data.get("country", ""),
                    language=data.get("language", ""),
                    currency=data.get("currency", ""),
                    wallet_action=data.get("wallet_action", ""),
                    wallet_amount_minor=amount,
                )
                response = RedirectResponse("/settings", status_code=303)
                _set_private_cookie(response, "steam_mcp_web", token, MANAGEMENT_SESSION_SECONDS)
                return response
            target = await auth.save_settings_and_issue_code(
                session_token=token,
                csrf_token=data.get("csrf_token", ""),
                country=data.get("country", ""),
                language=data.get("language", ""),
                currency=data.get("currency", ""),
                wallet_action=data.get("wallet_action", ""),
                wallet_amount_minor=amount,
            )
            response = RedirectResponse(target, status_code=303)
            _set_private_cookie(response, "steam_mcp_web", token, MANAGEMENT_SESSION_SECONDS)
            return response
        except (ValueError, OAuthError) as exc:
            error = exc.description if isinstance(exc, OAuthError) else "Wallet amount must be a whole number of minor units."
            context = await auth.web_session_context(token)
            if context is None:
                return JSONResponse({"error": "steam_login_required"}, status_code=401)
            return HTMLResponse(_settings_page(context, str(context["csrf_token"]), error), status_code=400)

    @app.get("/backlog", response_class=HTMLResponse)
    async def backlog_page(request: Request) -> Response:
        token = request.cookies.get("steam_mcp_web")
        if not token:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        context = await auth.web_session_context(token)
        if context is None:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        entries = await db.get_local_backlog(str(context["user_id"]))
        watches = await db.get_local_watchlist(str(context["user_id"]))
        tracking_rows = await db.get_price_tracking(str(context["user_id"]))
        watchlist = {int(row["appid"]) for row in watches if row.get("appid") is not None}
        tracked = {int(row["appid"]) for row in tracking_rows if row.get("appid") is not None}
        response = HTMLResponse(_backlog_page(str(context["csrf_token"]), entries, watchlist, tracked))
        _set_private_cookie(response, "steam_mcp_web", token, MANAGEMENT_SESSION_SECONDS)
        return response

    @app.post("/backlog")
    async def save_backlog(request: Request) -> Response:
        token = request.cookies.get("steam_mcp_web")
        if not token:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        if not request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded"):
            return JSONResponse({"error": "invalid_request"}, status_code=415)
        context = await auth.web_session_context(token, rotate_csrf=False)
        if context is None:
            return JSONResponse({"error": "steam_login_required"}, status_code=401)
        try:
            data = _form(await request.body())
            appid = int(data.get("appid", ""))
            if not 1 <= appid <= 2_147_483_647:
                raise ValueError("Steam AppID must be between 1 and 2,147,483,647.")
            state = data.get("state", "") or None
            allowed_states = {"want_to_play", "playing", "paused", "finished", "dropped", "completed_100"}
            if state is not None and state not in allowed_states:
                raise ValueError("Choose a valid backlog state.")
            note = data.get("note", "").strip() or None
            if note is not None and len(note) > 500:
                raise ValueError("Note must be 500 characters or fewer.")
            score_raw = data.get("user_score", "").strip()
            score = float(score_raw) if score_raw else None
            if score is not None and (not 0 <= score <= 10):
                raise ValueError("Score must be between 0 and 10.")
            priority_raw = data.get("priority", "").strip()
            priority = int(priority_raw) if priority_raw else None
            if priority is not None and not 0 <= priority <= 100:
                raise ValueError("Priority must be between 0 and 100.")
            await auth.save_backlog_entry(
                session_token=token,
                user_id=str(context["user_id"]),
                csrf_token=data.get("csrf_token", ""),
                appid=appid,
                state=state,
                note=note,
                user_score=score,
                priority=priority,
                watched=data.get("watched") == "yes",
                track_price=data.get("track_price") == "yes",
            )
            context = await auth.web_session_context(token)
            assert context is not None
            entries = await db.get_local_backlog(str(context["user_id"]))
            watches = await db.get_local_watchlist(str(context["user_id"]))
            tracking_rows = await db.get_price_tracking(str(context["user_id"]))
            watchlist = {int(row["appid"]) for row in watches if row.get("appid") is not None}
            tracked = {int(row["appid"]) for row in tracking_rows if row.get("appid") is not None}
            response = HTMLResponse(_backlog_page(str(context["csrf_token"]), entries, watchlist, tracked))
            _set_private_cookie(response, "steam_mcp_web", token, MANAGEMENT_SESSION_SECONDS)
            return response
        except (ValueError, OAuthError) as exc:
            error = exc.description if isinstance(exc, OAuthError) else str(exc)
            context = await auth.web_session_context(token)
            if context is None:
                return JSONResponse({"error": "steam_login_required"}, status_code=401)
            entries = await db.get_local_backlog(str(context["user_id"]))
            watches = await db.get_local_watchlist(str(context["user_id"]))
            tracking_rows = await db.get_price_tracking(str(context["user_id"]))
            watchlist = {int(row["appid"]) for row in watches if row.get("appid") is not None}
            tracked = {int(row["appid"]) for row in tracking_rows if row.get("appid") is not None}
            return HTMLResponse(
                _backlog_page(str(context["csrf_token"]), entries, watchlist, tracked, error), status_code=400
            )

    @app.post("/oauth/token")
    async def token(request: Request) -> JSONResponse:
        if not request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded"):
            return JSONResponse({"error": "invalid_request"}, status_code=415, headers={"Cache-Control": "no-store"})
        try:
            data = _form(await request.body())
            grant = data.get("grant_type", "")
            if grant == "authorization_code":
                result = await auth.exchange_code(
                    client_id=data.get("client_id", ""),
                    redirect_uri=data.get("redirect_uri", ""),
                    code=data.get("code", ""),
                    verifier=data.get("code_verifier", ""),
                    resource=data.get("resource", ""),
                )
            elif grant == "refresh_token":
                result = await auth.rotate_refresh_token(
                    client_id=data.get("client_id", ""),
                    refresh_token=data.get("refresh_token", ""),
                    resource=data.get("resource", ""),
                )
            else:
                raise OAuthError("unsupported_grant_type", "This grant type is not supported.")
            return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        except OAuthError as exc:
            return JSONResponse(
                {"error": exc.code, "error_description": exc.description},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )

    app.mount("/", ProtectedMCP(mcp_http, auth, config))
    return app


app = create_app()
