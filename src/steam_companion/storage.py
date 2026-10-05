from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiosqlite


SCHEMA_VERSION = 8

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    steam_id TEXT NOT NULL UNIQUE CHECK(length(steam_id) = 17),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_transactions (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    client_state TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    browser_hash TEXT NOT NULL,
    user_id TEXT REFERENCES users(id),
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER
);
CREATE TABLE IF NOT EXISTS steam_openid_states (
    state_hash TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES oauth_transactions(id) ON DELETE CASCADE,
    browser_hash TEXT NOT NULL,
    return_to TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER
);
CREATE TABLE IF NOT EXISTS openid_nonces (
    nonce TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS authorization_codes (
    code_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    client_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    replaced_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_refresh_family ON refresh_tokens(family_id);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    store_country_code TEXT NOT NULL,
    preferred_language TEXT NOT NULL,
    expected_currency TEXT NOT NULL,
    wallet_amount_minor INTEGER,
    wallet_currency TEXT,
    wallet_updated_at INTEGER,
    wallet_source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS web_sessions (
    session_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    transaction_id TEXT NOT NULL REFERENCES oauth_transactions(id) ON DELETE CASCADE,
    csrf_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS game_identity (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_game_id TEXT NOT NULL,
    UNIQUE(provider, provider_game_id)
);
PRAGMA user_version = 1;
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS library_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    snapshot_date TEXT NOT NULL,
    total_games INTEGER NOT NULL,
    played_games INTEGER NOT NULL,
    total_playtime_minutes INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(user_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS ix_library_snapshots_user_date ON library_snapshots(user_id, snapshot_date DESC);
PRAGMA user_version = 2;
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS store_price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appid INTEGER NOT NULL,
    country_code TEXT NOT NULL,
    currency TEXT NOT NULL,
    price_state TEXT NOT NULL CHECK(price_state IN ('priced','free')),
    base_price_minor INTEGER,
    final_price_minor INTEGER NOT NULL,
    discount_percent INTEGER,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(appid,country_code,currency,fetched_at)
);
CREATE INDEX IF NOT EXISTS ix_store_price_history ON store_price_snapshots(appid,country_code,currency,fetched_at DESC);
PRAGMA user_version = 3;
"""

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS activity_state (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL CHECK(appid > 0),
    playtime_minutes INTEGER NOT NULL CHECK(playtime_minutes >= 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY(user_id, appid)
);
CREATE TABLE IF NOT EXISTS activity_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL CHECK(appid > 0),
    delta_minutes INTEGER NOT NULL CHECK(delta_minutes > 0),
    previous_playtime_minutes INTEGER NOT NULL,
    current_playtime_minutes INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(user_id, appid, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_activity_deltas_user_observed
    ON activity_deltas(user_id, observed_at DESC, appid);
PRAGMA user_version = 4;
"""

SCHEMA_V5 = """
ALTER TABLE game_identity ADD COLUMN steam_appid INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS ux_game_identity_steam_appid
    ON game_identity(steam_appid) WHERE steam_appid IS NOT NULL;
CREATE TABLE IF NOT EXISTS local_game_state (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id TEXT NOT NULL REFERENCES game_identity(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('want_to_play','playing','paused','finished','dropped','completed_100')),
    note TEXT CHECK(note IS NULL OR length(note) <= 500),
    user_score REAL CHECK(user_score IS NULL OR (user_score >= 0 AND user_score <= 10)),
    priority INTEGER CHECK(priority IS NULL OR (priority >= 0 AND priority <= 100)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, game_id)
);
CREATE INDEX IF NOT EXISTS ix_local_game_state_user_state
    ON local_game_state(user_id, state, priority DESC);
CREATE TABLE IF NOT EXISTS watchlist (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id TEXT NOT NULL REFERENCES game_identity(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY(user_id, game_id)
);
CREATE INDEX IF NOT EXISTS ix_watchlist_user_added ON watchlist(user_id, added_at DESC);
CREATE TABLE IF NOT EXISTS management_sessions (
    session_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_management_sessions_user_expiry
    ON management_sessions(user_id, expires_at);
PRAGMA user_version = 5;
"""

SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS price_tracking (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id TEXT NOT NULL REFERENCES game_identity(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY(user_id, game_id)
);
CREATE INDEX IF NOT EXISTS ix_price_tracking_user_added ON price_tracking(user_id, added_at DESC);
PRAGMA user_version = 6;
"""

SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS library_presence (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL CHECK(appid > 0),
    playtime_minutes INTEGER NOT NULL CHECK(playtime_minutes >= 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY(user_id, appid)
);
CREATE TABLE IF NOT EXISTS library_observation_state (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'game_acquired','game_first_played','playtime_changed','achievement_unlocked',
        'wishlist_added','wishlist_removed','sale_started','price_changed','new_personal_observed_low'
    )),
    appid INTEGER NOT NULL CHECK(appid > 0),
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(user_id,event_type,appid,observed_at)
);
CREATE INDEX IF NOT EXISTS ix_event_journal_user_observed
    ON event_journal(user_id,observed_at DESC,id DESC);
PRAGMA user_version = 7;
"""

SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS wishlist_presence (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL CHECK(appid > 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY(user_id,appid)
);
CREATE TABLE IF NOT EXISTS wishlist_observation_state (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS achievement_unlocks (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL CHECK(appid > 0),
    api_name TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(user_id,appid,api_name)
);
CREATE TABLE IF NOT EXISTS achievement_observation_state (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    appid INTEGER NOT NULL CHECK(appid > 0),
    observed_at TEXT NOT NULL,
    PRIMARY KEY(user_id,appid)
);
PRAGMA user_version = 8;
"""


class Database:
    """Small repository boundary around SQLite; services do not depend on SQL details."""

    def __init__(self, database_url: str):
        self.path = self._path_from_url(database_url)
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _path_from_url(database_url: str) -> str:
        raw = database_url.removeprefix("sqlite+aiosqlite:///")
        if raw == ":memory:":
            return raw
        path = Path(unquote(raw))
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    async def connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.path, timeout=10)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 10000")
        return db

    @contextlib.asynccontextmanager
    async def connection(self):
        db = await self.connect()
        try:
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute("PRAGMA journal_mode = WAL")
                cursor = await db.execute("PRAGMA user_version")
                version = (await cursor.fetchone())[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError("Database schema is newer than this application")
                migrations = {
                    0: SCHEMA_V1, 1: SCHEMA_V2, 2: SCHEMA_V3, 3: SCHEMA_V4,
                    4: SCHEMA_V5, 5: SCHEMA_V6, 6: SCHEMA_V7, 7: SCHEMA_V8,
                }
                while version < SCHEMA_VERSION:
                    script = migrations[version]
                    try:
                        # sqlite3.executescript commits any pending transaction before
                        # running, so begin and commit must be part of the script itself.
                        await db.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
                    except Exception:
                        await db.rollback()
                        raise
                    version += 1

    async def readiness(self) -> bool:
        async with self.connection() as db:
            cursor = await db.execute("SELECT 1")
            return (await cursor.fetchone())[0] == 1

    async def set_manual_wallet(
        self, user_id: str, amount_minor: int | None, currency: str | None, updated_at: int | None
    ) -> None:
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute(
                    "UPDATE user_settings SET wallet_amount_minor=?, wallet_currency=?, "
                    "wallet_updated_at=?, wallet_source='manual' WHERE user_id=?",
                    (amount_minor, currency, updated_at, user_id),
                )
                await db.commit()

    async def record_library_snapshot(
        self,
        user_id: str,
        snapshot_date: str,
        total_games: int,
        played_games: int,
        total_playtime_minutes: int,
        fetched_at: str,
    ) -> None:
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute(
                    "INSERT INTO library_snapshots "
                    "(user_id,snapshot_date,total_games,played_games,total_playtime_minutes,fetched_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,snapshot_date) DO UPDATE SET "
                    "total_games=excluded.total_games,played_games=excluded.played_games,"
                    "total_playtime_minutes=excluded.total_playtime_minutes,fetched_at=excluded.fetched_at",
                    (user_id, snapshot_date, total_games, played_games, total_playtime_minutes, fetched_at),
                )
                await db.commit()

    async def record_library_observation(
        self,
        user_id: str,
        snapshot_date: str,
        total_games: int,
        played_games: int,
        total_playtime_minutes: int,
        fetched_at: str,
        activity: list[tuple[int, int]],
    ) -> None:
        """Atomically persist a daily snapshot and only confirmed playtime increases."""
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        "SELECT observed_at FROM library_observation_state WHERE user_id=?", (user_id,)
                    )
                    last_observed = await cursor.fetchone()
                    if last_observed is not None and str(last_observed["observed_at"]) >= fetched_at:
                        await db.rollback()
                        return
                    await db.execute(
                        "INSERT INTO library_snapshots "
                        "(user_id,snapshot_date,total_games,played_games,total_playtime_minutes,fetched_at) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,snapshot_date) DO UPDATE SET "
                        "total_games=excluded.total_games,played_games=excluded.played_games,"
                        "total_playtime_minutes=excluded.total_playtime_minutes,fetched_at=excluded.fetched_at",
                        (user_id, snapshot_date, total_games, played_games, total_playtime_minutes, fetched_at),
                    )
                    cursor = await db.execute(
                        "SELECT appid,playtime_minutes FROM activity_state WHERE user_id=?", (user_id,)
                    )
                    previous = {int(row["appid"]): int(row["playtime_minutes"]) for row in await cursor.fetchall()}
                    cursor = await db.execute(
                        "SELECT appid,playtime_minutes FROM library_presence WHERE user_id=?", (user_id,)
                    )
                    previous_presence = {
                        int(row["appid"]): int(row["playtime_minutes"]) for row in await cursor.fetchall()
                    }
                    cursor = await db.execute(
                        "SELECT 1 FROM library_observation_state WHERE user_id=?", (user_id,)
                    )
                    has_baseline = await cursor.fetchone() is not None
                    unique_activity = dict(activity)
                    state_rows: list[tuple[str, int, int, str]] = []
                    delta_rows: list[tuple[str, int, int, int, int, str]] = []
                    event_rows: list[tuple[str, str, int, str, str]] = []
                    for appid, current_minutes in unique_activity.items():
                        old_minutes = previous.get(appid)
                        old_present_minutes = previous_presence.get(appid)
                        if old_minutes != current_minutes:
                            state_rows.append((user_id, appid, current_minutes, fetched_at))
                        if old_present_minutes is None:
                            if has_baseline:
                                event_rows.append((user_id, "game_acquired", appid, fetched_at, "{}"))
                        elif old_present_minutes == 0 and current_minutes > 0:
                            event_rows.append((user_id, "game_first_played", appid, fetched_at, json.dumps({
                                "previous_playtime_minutes": 0, "current_playtime_minutes": current_minutes,
                            }, separators=(",", ":"))))
                        if old_minutes is not None and current_minutes > old_minutes:
                            delta_rows.append((
                                user_id, appid, current_minutes - old_minutes, old_minutes, current_minutes, fetched_at
                            ))
                            event_rows.append((user_id, "playtime_changed", appid, fetched_at, json.dumps({
                                "delta_minutes": current_minutes - old_minutes,
                                "previous_playtime_minutes": old_minutes,
                                "current_playtime_minutes": current_minutes,
                            }, separators=(",", ":"))))
                    if state_rows:
                        await db.executemany(
                            "INSERT INTO activity_state(user_id,appid,playtime_minutes,observed_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(user_id,appid) DO UPDATE SET playtime_minutes=excluded.playtime_minutes,observed_at=excluded.observed_at",
                            state_rows,
                        )
                    if delta_rows:
                        await db.executemany(
                            "INSERT INTO activity_deltas(user_id,appid,delta_minutes,previous_playtime_minutes,"
                            "current_playtime_minutes,observed_at) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(user_id,appid,observed_at) DO NOTHING",
                            delta_rows,
                        )
                    if event_rows:
                        await db.executemany(
                            "INSERT INTO event_journal(user_id,event_type,appid,observed_at,payload_json) "
                            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,event_type,appid,observed_at) DO NOTHING",
                            event_rows,
                        )
                    await db.execute("DELETE FROM library_presence WHERE user_id=?", (user_id,))
                    await db.executemany(
                        "INSERT INTO library_presence(user_id,appid,playtime_minutes,observed_at) VALUES(?,?,?,?)",
                        [(user_id, appid, minutes, fetched_at) for appid, minutes in unique_activity.items()],
                    )
                    await db.execute(
                        "INSERT INTO library_observation_state(user_id,observed_at) VALUES(?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET observed_at=excluded.observed_at",
                        (user_id, fetched_at),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def get_activity_deltas(self, user_id: str, limit: int | None) -> list[dict[str, object]]:
        async with self.connection() as db:
            sql = (
                "SELECT appid,delta_minutes,previous_playtime_minutes,current_playtime_minutes,observed_at "
                "FROM activity_deltas WHERE user_id=? ORDER BY observed_at DESC,id DESC"
            )
            params: tuple[object, ...] = (user_id,)
            if limit is not None:
                sql += " LIMIT ?"
                params += (limit,)
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_event_journal(self, user_id: str, limit: int | None) -> list[dict[str, object]]:
        async with self.connection() as db:
            sql = (
                "SELECT event_type,appid,observed_at,payload_json FROM event_journal "
                "WHERE user_id=? ORDER BY observed_at DESC,id DESC"
            )
            params: tuple[object, ...] = (user_id,)
            if limit is not None:
                sql += " LIMIT ?"
                params += (limit,)
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    async def record_wishlist_observation(self, user_id: str, appids: list[int], observed_at: str) -> None:
        current = set(appids)
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        "SELECT observed_at FROM wishlist_observation_state WHERE user_id=?", (user_id,)
                    )
                    marker = await cursor.fetchone()
                    if marker is not None and str(marker["observed_at"]) >= observed_at:
                        await db.rollback()
                        return
                    cursor = await db.execute(
                        "SELECT appid FROM wishlist_presence WHERE user_id=?", (user_id,)
                    )
                    previous = {int(row["appid"]) for row in await cursor.fetchall()}
                    if marker is not None:
                        event_rows = [
                            (user_id, event_type, appid, observed_at, "{}")
                            for event_type, appid in (
                                [("wishlist_added", appid) for appid in current - previous]
                                + [("wishlist_removed", appid) for appid in previous - current]
                            )
                        ]
                        if event_rows:
                            await db.executemany(
                                "INSERT INTO event_journal(user_id,event_type,appid,observed_at,payload_json) "
                                "VALUES(?,?,?,?,?) ON CONFLICT(user_id,event_type,appid,observed_at) DO NOTHING",
                                event_rows,
                            )
                    await db.execute("DELETE FROM wishlist_presence WHERE user_id=?", (user_id,))
                    if current:
                        await db.executemany(
                            "INSERT INTO wishlist_presence(user_id,appid,observed_at) VALUES(?,?,?)",
                            [(user_id, appid, observed_at) for appid in sorted(current)],
                        )
                    await db.execute(
                        "INSERT INTO wishlist_observation_state(user_id,observed_at) VALUES(?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET observed_at=excluded.observed_at",
                        (user_id, observed_at),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def record_achievement_observation(
        self, user_id: str, appid: int, unlocked_api_names: list[str], observed_at: str
    ) -> None:
        current = {name for name in unlocked_api_names if name}
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        "SELECT observed_at FROM achievement_observation_state WHERE user_id=? AND appid=?",
                        (user_id, appid),
                    )
                    marker = await cursor.fetchone()
                    if marker is not None and str(marker["observed_at"]) >= observed_at:
                        await db.rollback()
                        return
                    cursor = await db.execute(
                        "SELECT api_name FROM achievement_unlocks WHERE user_id=? AND appid=?",
                        (user_id, appid),
                    )
                    previous = {str(row["api_name"]) for row in await cursor.fetchall()}
                    newly_unlocked = current - previous if marker is not None else set()
                    if newly_unlocked:
                        await db.execute(
                            "INSERT INTO event_journal(user_id,event_type,appid,observed_at,payload_json) "
                            "VALUES(?,'achievement_unlocked',?,?,?) "
                            "ON CONFLICT(user_id,event_type,appid,observed_at) DO NOTHING",
                            (
                                user_id,
                                appid,
                                observed_at,
                                json.dumps({"api_names": sorted(newly_unlocked)}, separators=(",", ":")),
                            ),
                        )
                    await db.execute(
                        "DELETE FROM achievement_unlocks WHERE user_id=? AND appid=?", (user_id, appid)
                    )
                    if current:
                        await db.executemany(
                            "INSERT INTO achievement_unlocks(user_id,appid,api_name,observed_at) VALUES(?,?,?,?)",
                            [(user_id, appid, name, observed_at) for name in sorted(current)],
                        )
                    await db.execute(
                        "INSERT INTO achievement_observation_state(user_id,appid,observed_at) VALUES(?,?,?) "
                        "ON CONFLICT(user_id,appid) DO UPDATE SET observed_at=excluded.observed_at",
                        (user_id, appid, observed_at),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def get_local_backlog(self, user_id: str) -> list[dict[str, object]]:
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT gi.steam_appid AS appid,lgs.state,lgs.note,lgs.user_score,lgs.priority,lgs.updated_at "
                "FROM local_game_state lgs JOIN game_identity gi ON gi.id=lgs.game_id "
                "WHERE lgs.user_id=? AND gi.provider='steam' "
                "ORDER BY COALESCE(lgs.priority,0) DESC,lgs.updated_at DESC",
                (user_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_local_watchlist(self, user_id: str) -> list[dict[str, object]]:
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT gi.steam_appid AS appid,w.added_at "
                "FROM watchlist w JOIN game_identity gi ON gi.id=w.game_id "
                "WHERE w.user_id=? AND gi.provider='steam' ORDER BY w.added_at DESC",
                (user_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_price_tracking(self, user_id: str) -> list[dict[str, object]]:
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT gi.steam_appid AS appid,p.added_at FROM price_tracking p "
                "JOIN game_identity gi ON gi.id=p.game_id WHERE p.user_id=? AND gi.provider='steam' "
                "ORDER BY p.added_at DESC",
                (user_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def save_local_game(
        self,
        user_id: str,
        appid: int,
        *,
        state: str | None,
        note: str | None,
        user_score: float | None,
        priority: int | None,
        watched: bool,
        track_price: bool,
        updated_at: str,
        session_hash: str,
        csrf_hash: str,
        next_csrf_hash: str,
        allowed_steam_ids: set[str],
    ) -> None:
        """Atomically update explicit backlog state and the separate local watchlist."""
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        "SELECT ms.user_id,ms.csrf_hash,ms.expires_at,u.steam_id FROM management_sessions ms "
                        "JOIN users u ON u.id=ms.user_id WHERE ms.session_hash=?",
                        (session_hash,),
                    )
                    session = await cursor.fetchone()
                    now = int(time.time())
                    if (
                        session is None
                        or session["expires_at"] < now
                        or session["steam_id"] not in allowed_steam_ids
                        or not hmac.compare_digest(str(session["csrf_hash"]), csrf_hash)
                    ):
                        await db.rollback()
                        raise ValueError("The backlog form expired. Reload the page and try again.")
                    if session["user_id"] != user_id:
                        await db.rollback()
                        raise ValueError("The management session is invalid.")
                    if track_price:
                        cursor = await db.execute(
                            "SELECT 1 FROM price_tracking WHERE user_id=? AND game_id=?",
                            (user_id, f"steam:{appid}"),
                        )
                        if await cursor.fetchone() is None:
                            cursor = await db.execute(
                                "SELECT COUNT(*) FROM price_tracking WHERE user_id=?", (user_id,)
                            )
                            if int((await cursor.fetchone())[0]) >= 100:
                                await db.rollback()
                                raise ValueError("Track price history for at most 100 games.")
                    await db.execute(
                        "UPDATE management_sessions SET csrf_hash=?,expires_at=? WHERE session_hash=?",
                        (next_csrf_hash, now + 30 * 24 * 60 * 60, session_hash),
                    )
                    game_id = f"steam:{appid}"
                    await db.execute(
                        "INSERT INTO game_identity(id,provider,provider_game_id,steam_appid) VALUES(?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET steam_appid=excluded.steam_appid",
                        (game_id, "steam", str(appid), appid),
                    )
                    if state is None:
                        await db.execute(
                            "DELETE FROM local_game_state WHERE user_id=? AND game_id=?", (user_id, game_id)
                        )
                    else:
                        await db.execute(
                            "INSERT INTO local_game_state(user_id,game_id,state,note,user_score,priority,updated_at) "
                            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,game_id) DO UPDATE SET "
                            "state=excluded.state,note=excluded.note,user_score=excluded.user_score,"
                            "priority=excluded.priority,updated_at=excluded.updated_at",
                            (user_id, game_id, state, note, user_score, priority, updated_at),
                        )
                    if watched:
                        await db.execute(
                            "INSERT INTO watchlist(user_id,game_id,added_at) VALUES(?,?,?) "
                            "ON CONFLICT(user_id,game_id) DO NOTHING",
                            (user_id, game_id, updated_at),
                        )
                    else:
                        await db.execute("DELETE FROM watchlist WHERE user_id=? AND game_id=?", (user_id, game_id))
                    if track_price:
                        await db.execute(
                            "INSERT INTO price_tracking(user_id,game_id,added_at) VALUES(?,?,?) "
                            "ON CONFLICT(user_id,game_id) DO NOTHING",
                            (user_id, game_id, updated_at),
                        )
                    else:
                        await db.execute("DELETE FROM price_tracking WHERE user_id=? AND game_id=?", (user_id, game_id))
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def get_library_snapshots(self, user_id: str, limit: int) -> list[dict[str, object]]:
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT snapshot_date,total_games,played_games,total_playtime_minutes,fetched_at "
                "FROM library_snapshots WHERE user_id=? ORDER BY snapshot_date DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def record_price_snapshot(
        self,
        appid: int,
        country_code: str,
        currency: str,
        price_state: str,
        base_price_minor: int | None,
        final_price_minor: int,
        discount_percent: int | None,
        fetched_at: str,
        source: str,
    ) -> None:
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute(
                    "INSERT INTO store_price_snapshots "
                    "(appid,country_code,currency,price_state,base_price_minor,final_price_minor,discount_percent,fetched_at,source) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(appid,country_code,currency,fetched_at) DO UPDATE SET "
                    "price_state=excluded.price_state,base_price_minor=excluded.base_price_minor,"
                    "final_price_minor=excluded.final_price_minor,discount_percent=excluded.discount_percent,source=excluded.source",
                    (appid, country_code.upper(), currency.upper(), price_state, base_price_minor, final_price_minor,
                     discount_percent, fetched_at, source),
                )
                await db.commit()

    async def record_price_snapshots(
        self,
        snapshots: list[tuple[int, str, str, str, int | None, int, int | None, str, str]],
    ) -> None:
        """Persist a batch of current regional offers in one transaction."""
        if not snapshots:
            return
        async with self._write_lock:
            async with self.connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    appids = sorted({snapshot[0] for snapshot in snapshots})
                    placeholders = ",".join("?" for _ in appids)
                    event_rows: list[tuple[str, str, int, str, str]] = []
                    for appid, country, currency, state, _base, final, discount, fetched_at, _source in snapshots:
                        normalized_country = country.upper()
                        normalized_currency = currency.upper()
                        cursor = await db.execute(
                            "SELECT price_state,final_price_minor,discount_percent FROM store_price_snapshots "
                            "WHERE appid=? AND country_code=? AND currency=? AND fetched_at<? "
                            "ORDER BY fetched_at DESC,id DESC LIMIT 1",
                            (appid, normalized_country, normalized_currency, fetched_at),
                        )
                        previous = await cursor.fetchone()
                        if previous is None:
                            continue
                        cursor = await db.execute(
                            "SELECT MIN(final_price_minor) FROM store_price_snapshots "
                            "WHERE appid=? AND country_code=? AND currency=? AND fetched_at<?",
                            (appid, normalized_country, normalized_currency, fetched_at),
                        )
                        prior_low = (await cursor.fetchone())[0]
                        cursor = await db.execute(
                            "SELECT p.user_id FROM price_tracking p JOIN game_identity gi ON gi.id=p.game_id "
                            "WHERE gi.provider='steam' AND gi.steam_appid=?",
                            (appid,),
                        )
                        tracked_users = [str(row["user_id"]) for row in await cursor.fetchall()]
                        changed = (
                            str(previous["price_state"]) != state
                            or int(previous["final_price_minor"]) != final
                            or previous["discount_percent"] != discount
                        )
                        sale_started = (int(discount or 0) > 0 and int(previous["discount_percent"] or 0) <= 0)
                        new_low = prior_low is not None and final < int(prior_low)
                        for tracked_user in tracked_users:
                            if changed:
                                event_rows.append((tracked_user, "price_changed", appid, fetched_at, json.dumps({
                                    "country_code": normalized_country,
                                    "currency": normalized_currency,
                                    "previous_price_minor": int(previous["final_price_minor"]),
                                    "current_price_minor": final,
                                }, separators=(",", ":"))))
                            if sale_started:
                                event_rows.append((tracked_user, "sale_started", appid, fetched_at, json.dumps({
                                    "country_code": normalized_country,
                                    "currency": normalized_currency,
                                    "discount_percent": discount,
                                }, separators=(",", ":"))))
                            if new_low:
                                event_rows.append((tracked_user, "new_personal_observed_low", appid, fetched_at, json.dumps({
                                    "country_code": normalized_country,
                                    "currency": normalized_currency,
                                    "final_price_minor": final,
                                    "previous_observed_low_minor": int(prior_low),
                                }, separators=(",", ":"))))
                    if event_rows:
                        await db.executemany(
                            "INSERT INTO event_journal(user_id,event_type,appid,observed_at,payload_json) "
                            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,event_type,appid,observed_at) DO NOTHING",
                            event_rows,
                        )
                    await db.executemany(
                        "INSERT INTO store_price_snapshots "
                        "(appid,country_code,currency,price_state,base_price_minor,final_price_minor,discount_percent,fetched_at,source) "
                        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(appid,country_code,currency,fetched_at) DO UPDATE SET "
                        "price_state=excluded.price_state,base_price_minor=excluded.base_price_minor,"
                        "final_price_minor=excluded.final_price_minor,discount_percent=excluded.discount_percent,source=excluded.source",
                        [
                            (appid, country.upper(), currency.upper(), state, base, final, discount, fetched_at, source)
                            for appid, country, currency, state, base, final, discount, fetched_at, source in snapshots
                        ],
                    )
                    await db.execute(
                        "DELETE FROM store_price_snapshots WHERE id IN (SELECT id FROM ("
                        "SELECT id,ROW_NUMBER() OVER (PARTITION BY appid,country_code,currency "
                        "ORDER BY fetched_at DESC,id DESC) AS row_number FROM store_price_snapshots "
                        f"WHERE appid IN ({placeholders})) WHERE row_number>365)",
                        appids,
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def get_price_snapshots_batch(
        self, appids: list[int], country_code: str, currency: str, limit_per_appid: int
    ) -> dict[int, list[dict[str, object]]]:
        if not appids:
            return {}
        placeholders = ",".join("?" for _ in appids)
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT appid,country_code,currency,price_state,base_price_minor,final_price_minor,"
                "discount_percent,fetched_at,source FROM ("
                "SELECT id,appid,country_code,currency,price_state,base_price_minor,final_price_minor,"
                "discount_percent,fetched_at,source,ROW_NUMBER() OVER (PARTITION BY appid ORDER BY fetched_at DESC) AS rank "
                f"FROM store_price_snapshots WHERE appid IN ({placeholders}) AND country_code=? AND currency=?"
                ") WHERE rank<=? ORDER BY appid,fetched_at DESC",
                (*appids, country_code.upper(), currency.upper(), limit_per_appid),
            )
            rows = await cursor.fetchall()
        grouped: dict[int, list[dict[str, object]]] = {appid: [] for appid in appids}
        for row in rows:
            grouped[int(row["appid"])].append(dict(row))
        return grouped

    async def get_price_snapshots(
        self, appid: int, country_code: str, currency: str, limit: int
    ) -> list[dict[str, object]]:
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT appid,country_code,currency,price_state,base_price_minor,final_price_minor,"
                "discount_percent,fetched_at,source FROM store_price_snapshots "
                "WHERE appid=? AND country_code=? AND currency=? ORDER BY fetched_at DESC LIMIT ?",
                (appid, country_code.upper(), currency.upper(), limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_tracked_price_snapshots(self, appids: list[int], limit_per_region: int = 365) -> dict[int, list[dict[str, object]]]:
        """Return every tracked region/currency series, newest observations first."""
        if not appids:
            return {}
        placeholders = ",".join("?" for _ in appids)
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT appid,country_code,currency,price_state,base_price_minor,final_price_minor,"
                "discount_percent,fetched_at,source FROM ("
                "SELECT id,appid,country_code,currency,price_state,base_price_minor,final_price_minor,"
                "discount_percent,fetched_at,source,ROW_NUMBER() OVER ("
                "PARTITION BY appid,country_code,currency ORDER BY fetched_at DESC,id DESC) AS rank "
                f"FROM store_price_snapshots WHERE appid IN ({placeholders})"
                ") WHERE rank<=? ORDER BY appid,country_code,currency,fetched_at DESC,id DESC",
                (*appids, limit_per_region),
            )
            rows = await cursor.fetchall()
        grouped: dict[int, list[dict[str, object]]] = {appid: [] for appid in appids}
        for row in rows:
            grouped[int(row["appid"])].append(dict(row))
        return grouped
