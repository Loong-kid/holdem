"""Optional PostgreSQL persistence for hand replays.

The whole module is a no-op unless a DATABASE_URL environment variable is set
(Render injects this when you link a Postgres instance). That way the app runs
exactly as before locally with in-memory storage, and automatically persists in
the cloud. A finished hand is stored as one row with its events in a JSONB column.
"""

import json
import os
from urllib.parse import unquote, urlsplit

try:
    import asyncpg
except ImportError:           # local dev without the driver installed
    asyncpg = None

_pool = None
_last_error = None       # why init failed last time (shown at /health)


def status() -> dict:
    """Diagnostic snapshot for /health - safe to expose (no secrets)."""
    return {
        "db": _pool is not None,
        "persistence": "postgresql" if _pool is not None else "in-memory",
        "driver_installed": asyncpg is not None,
        "url_present": bool(os.environ.get("DATABASE_URL")),
        "error": _last_error,
    }


def _conn_kwargs(url: str) -> dict:
    """Split a DATABASE_URL into explicit asyncpg arguments.

    We parse it ourselves (instead of letting asyncpg parse the DSN) so that
    special characters in the password - like '!' - don't trip up the parser.
    """
    p = urlsplit(url)
    return {
        "user": unquote(p.username) if p.username else None,
        "password": unquote(p.password) if p.password else None,
        "host": p.hostname,
        "port": p.port or 5432,
        "database": (p.path or "/postgres").lstrip("/") or "postgres",
    }


async def _make_pool(url):
    """Create the pool, retrying with SSL for managed providers (Supabase etc.).

    statement_cache_size=0 keeps us compatible with connection poolers (pgbouncer)
    that Supabase and others put in front of Postgres.
    """
    kw = _conn_kwargs(url)
    try:
        return await asyncpg.create_pool(
            min_size=1, max_size=5, statement_cache_size=0, **kw)
    except Exception:
        # Managed providers (Supabase, etc.) require SSL. Using the 'require'
        # string lets asyncpg build and manage the TLS connection itself
        # (encrypt without CA verification, correct SNI) - passing a hand-built
        # SSLContext tripped asyncpg's hostname handling.
        return await asyncpg.create_pool(
            min_size=1, max_size=5, statement_cache_size=0, ssl="require", **kw)


async def init():
    """Connect (if configured) and make sure the table exists. Returns enabled?."""
    global _pool, _last_error
    _last_error = None
    url = os.environ.get("DATABASE_URL")
    if asyncpg is None:
        _last_error = "asyncpg driver not installed"
        return False
    if not url:
        _last_error = "DATABASE_URL not set"
        return False
    try:
        _pool = await _make_pool(url)
        async with _pool.acquire() as con:
            await con.execute(
                """
                CREATE TABLE IF NOT EXISTS hands (
                    id          SERIAL PRIMARY KEY,
                    room        TEXT NOT NULL,
                    hand_number INT,
                    played_at   TIMESTAMPTZ DEFAULT now(),
                    title       TEXT,
                    events      JSONB
                )
                """
            )
            await con.execute(
                "CREATE INDEX IF NOT EXISTS idx_hands_room ON hands (room, id DESC)"
            )
        return True
    except Exception as e:           # bad URL / unreachable -> fall back to memory
        _last_error = repr(e)
        print("DB init failed, using in-memory storage:", repr(e), flush=True)
        _pool = None
        return False


async def close():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def enabled() -> bool:
    return _pool is not None


async def save_hand(room: str, number: int, title: str, events: list):
    if not _pool:
        return
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO hands (room, hand_number, title, events) "
            "VALUES ($1, $2, $3, $4::jsonb)",
            room, number, title, json.dumps(events),
        )


async def list_hands(room: str, limit: int = 30) -> list[dict]:
    """Most recent hands first: [{number(=db id), title}, ...]."""
    if not _pool:
        return []
    async with _pool.acquire() as con:
        rows = await con.fetch(
            "SELECT id, title FROM hands WHERE room = $1 ORDER BY id DESC LIMIT $2",
            room, limit,
        )
    return [{"number": r["id"], "title": r["title"]} for r in rows]


async def get_hand(room: str, hand_id: int) -> dict | None:
    if not _pool:
        return None
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id, events FROM hands WHERE id = $1 AND room = $2", hand_id, room
        )
    if not row:
        return None
    events = row["events"]
    if isinstance(events, str):       # asyncpg returns jsonb as text
        events = json.loads(events)
    return {"number": row["id"], "events": events}
