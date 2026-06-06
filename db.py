"""Optional PostgreSQL persistence for hand replays (via psycopg 3).

The whole module is a no-op unless a DATABASE_URL environment variable is set
(Render injects this when you link a Postgres instance). Locally, with no URL,
the app keeps everything in memory exactly as before.

We use psycopg (libpq-based) rather than asyncpg because it connects cleanly to
managed providers like Supabase's connection pooler, and we build the connection
info from keyword fields so special characters in the password are never a problem.
"""

import os
from urllib.parse import unquote, urlsplit

try:
    import psycopg
    from psycopg.types.json import Jsonb
    from psycopg_pool import AsyncConnectionPool
except ImportError:           # local dev without the driver installed
    psycopg = None
    AsyncConnectionPool = None

_pool = None
_last_error = None       # why init failed last time (shown at /health)


def status() -> dict:
    """Diagnostic snapshot for /health - safe to expose (no secrets)."""
    return {
        "db": _pool is not None,
        "persistence": "postgresql" if _pool is not None else "in-memory",
        "driver_installed": psycopg is not None,
        "url_present": bool(os.environ.get("DATABASE_URL")),
        "error": _last_error,
    }


def _conninfo(url: str) -> str:
    """Turn a DATABASE_URL into a libpq keyword conninfo string.

    Parsing it into explicit fields (instead of feeding the raw URL to the
    driver) means special characters in the password never need URL-encoding.
    """
    p = urlsplit(url)
    params = {
        "host": p.hostname,
        "port": p.port or 5432,
        "dbname": (p.path or "/postgres").lstrip("/") or "postgres",
        "sslmode": "require",
    }
    if p.username:
        params["user"] = unquote(p.username)
    if p.password:
        params["password"] = unquote(p.password)
    return psycopg.conninfo.make_conninfo(**params)


async def init():
    """Connect (if configured) and make sure the table exists. Returns enabled?."""
    global _pool, _last_error
    _last_error = None
    url = os.environ.get("DATABASE_URL")
    if psycopg is None or AsyncConnectionPool is None:
        _last_error = "psycopg driver not installed"
        return False
    if not url:
        _last_error = "DATABASE_URL not set"
        return False
    try:
        pool = AsyncConnectionPool(_conninfo(url), min_size=1, max_size=5, open=False)
        await pool.open(wait=True, timeout=15)
        async with pool.connection() as con:
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
        _pool = pool
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
    async with _pool.connection() as con:
        await con.execute(
            "INSERT INTO hands (room, hand_number, title, events) "
            "VALUES (%s, %s, %s, %s)",
            (room, number, title, Jsonb(events)),
        )


async def list_hands(room: str, limit: int = 30) -> list[dict]:
    """Most recent hands first: [{number(=db id), title}, ...]."""
    if not _pool:
        return []
    async with _pool.connection() as con:
        cur = await con.execute(
            "SELECT id, title FROM hands WHERE room = %s ORDER BY id DESC LIMIT %s",
            (room, limit),
        )
        rows = await cur.fetchall()
    return [{"number": r[0], "title": r[1]} for r in rows]


async def get_hand(room: str, hand_id: int) -> dict | None:
    if not _pool:
        return None
    async with _pool.connection() as con:
        cur = await con.execute(
            "SELECT id, events FROM hands WHERE id = %s AND room = %s",
            (hand_id, room),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {"number": row[0], "events": row[1]}   # psycopg returns jsonb as Python objects
