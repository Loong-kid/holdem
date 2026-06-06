"""Optional PostgreSQL persistence for hand replays (psycopg 3, sync-in-threads).

The whole module is a no-op unless a DATABASE_URL environment variable is set
(Render injects this when you link a Postgres instance). Locally, with no URL,
the app keeps everything in memory exactly as before.

We use *synchronous* psycopg run inside a thread pool (asyncio.to_thread) rather
than its async mode. Sync psycopg leans on libpq's own, robust hostname handling,
which sidesteps a bug where the async resolvers (both asyncpg and psycopg-async)
choke on some managed hostnames with "does not appear to be an IPv4/IPv6 address".
Writes are one-per-hand, so the tiny thread hop is negligible.
"""

import asyncio
import os
import socket
from urllib.parse import unquote, urlsplit

try:
    import psycopg
    from psycopg.types.json import Jsonb
    from psycopg_pool import ConnectionPool
except ImportError:           # local dev without the driver installed
    psycopg = None
    ConnectionPool = None

_pool = None
_last_error = None       # why init failed last time (shown at /health)
_diag = {}               # resolution diagnostics (shown at /health)


def status() -> dict:
    """Diagnostic snapshot for /health - safe to expose (no secrets)."""
    return {
        "db": _pool is not None,
        "persistence": "postgresql" if _pool is not None else "in-memory",
        "driver_installed": psycopg is not None,
        "psycopg_version": getattr(psycopg, "__version__", None),
        "url_present": bool(os.environ.get("DATABASE_URL")),
        "error": _last_error,
        "diag": _diag,
    }


def _conninfo(url: str) -> str:
    """Turn a DATABASE_URL into a libpq keyword conninfo string.

    Parsing it into explicit fields (instead of feeding the raw URL to the
    driver) means special characters in the password never need URL-encoding.
    """
    p = urlsplit(url)
    host = p.hostname
    port = p.port or 5432
    params = {
        "host": host,
        "port": port,
        "dbname": (p.path or "/postgres").lstrip("/") or "postgres",
        "sslmode": "require",
    }
    if p.username:
        params["user"] = unquote(p.username)
    if p.password:
        params["password"] = unquote(p.password)
    # Resolve the hostname to an IP ourselves and pass it as `hostaddr`. libpq then
    # connects to the IP directly (using `host` only for TLS SNI), so the driver
    # never runs its own hostname resolver - which on some platforms chokes with
    # "does not appear to be an IPv4 or IPv6 address".
    if host:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            all_ips = [i[4][0] for i in infos]
            ipv4 = [i[4][0] for i in infos if i[0] == socket.AF_INET]
            ip = ipv4[0] if ipv4 else (all_ips[0] if all_ips else None)
            _diag["resolved"] = all_ips
            if ip:
                params["hostaddr"] = ip
                _diag["hostaddr"] = ip
        except Exception as e:
            _diag["resolve_error"] = repr(e)
    _diag["conninfo_keys"] = sorted(k for k in params if k != "password")
    return psycopg.conninfo.make_conninfo(**params)


def _open_pool(url: str):
    conninfo = _conninfo(url)
    # A direct connect first surfaces real errors (auth, etc.) clearly, instead of
    # the pool hiding them behind a vague PoolTimeout. Also creates the table.
    with psycopg.connect(conninfo, connect_timeout=15) as con:
        con.execute(
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
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_hands_room ON hands (room, id DESC)"
        )
    pool = ConnectionPool(conninfo, min_size=1, max_size=5, open=False)
    pool.open(wait=True, timeout=15)
    return pool


async def init():
    """Connect (if configured) and make sure the table exists. Returns enabled?."""
    global _pool, _last_error
    _last_error = None
    url = os.environ.get("DATABASE_URL")
    if psycopg is None or ConnectionPool is None:
        _last_error = "psycopg driver not installed"
        return False
    if not url:
        _last_error = "DATABASE_URL not set"
        return False
    try:
        _pool = await asyncio.to_thread(_open_pool, url)
        return True
    except Exception as e:           # bad URL / unreachable -> fall back to memory
        _last_error = repr(e)
        print("DB init failed, using in-memory storage:", repr(e), flush=True)
        _pool = None
        return False


async def close():
    global _pool
    if _pool:
        pool, _pool = _pool, None
        await asyncio.to_thread(pool.close)


def enabled() -> bool:
    return _pool is not None


async def save_hand(room: str, number: int, title: str, events: list):
    if not _pool:
        return

    def _save():
        with _pool.connection() as con:
            con.execute(
                "INSERT INTO hands (room, hand_number, title, events) "
                "VALUES (%s, %s, %s, %s)",
                (room, number, title, Jsonb(events)),
            )

    await asyncio.to_thread(_save)


async def list_hands(room: str, limit: int = 30) -> list[dict]:
    """Most recent hands first: [{number(=db id), title}, ...]."""
    if not _pool:
        return []

    def _list():
        with _pool.connection() as con:
            cur = con.execute(
                "SELECT id, title FROM hands WHERE room = %s ORDER BY id DESC LIMIT %s",
                (room, limit),
            )
            return cur.fetchall()

    rows = await asyncio.to_thread(_list)
    return [{"number": r[0], "title": r[1]} for r in rows]


async def get_hand(room: str, hand_id: int) -> dict | None:
    if not _pool:
        return None

    def _get():
        with _pool.connection() as con:
            cur = con.execute(
                "SELECT id, events FROM hands WHERE id = %s AND room = %s",
                (hand_id, room),
            )
            return cur.fetchone()

    row = await asyncio.to_thread(_get)
    if not row:
        return None
    return {"number": row[0], "events": row[1]}   # psycopg returns jsonb as Python objects
