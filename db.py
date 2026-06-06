"""Optional PostgreSQL persistence for hand replays.

The whole module is a no-op unless a DATABASE_URL environment variable is set
(Render injects this when you link a Postgres instance). That way the app runs
exactly as before locally with in-memory storage, and automatically persists in
the cloud. A finished hand is stored as one row with its events in a JSONB column.
"""

import json
import os

try:
    import asyncpg
except ImportError:           # local dev without the driver installed
    asyncpg = None

_pool = None


async def init():
    """Connect (if configured) and make sure the table exists. Returns enabled?."""
    global _pool
    url = os.environ.get("DATABASE_URL")
    if not url or asyncpg is None:
        return False
    try:
        _pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
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
