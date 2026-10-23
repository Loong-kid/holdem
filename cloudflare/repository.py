"""D1 replay repository. Room state/outbox remain in Durable Object storage."""
import hashlib
import json


def event_payload(events):
    return json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def event_hash(events):
    return hashlib.sha256(event_payload(events).encode()).hexdigest()


def python_value(value):
    return value.to_py() if hasattr(value, "to_py") else value


class HandRepository:
    def __init__(self, database):
        self.database = database

    async def rows(self, sql, *parameters):
        result = python_value(await self.database.prepare(sql).bind(*parameters).all())
        return result["results"]

    async def save(self, hand):
        key = hand.get("source_key") or f"live:{hand['session_id']}:{hand['hand_number']}"
        digest = event_hash(hand["events"])
        await self.database.prepare(
            "INSERT INTO hands(source_key,session_id,room,hand_number,played_at,title,events,event_hash) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING"
        ).bind(key, hand["session_id"], hand["room"], hand["hand_number"],
               hand["played_at"], hand["title"], event_payload(hand["events"]), digest).run()
        existing = await self.rows("SELECT event_hash FROM hands WHERE source_key=?", key)
        if not existing or existing[0]["event_hash"] != digest:
            raise ValueError("Hand identity collision; retained in durable outbox")

    async def count(self):
        return (await self.rows("SELECT count(*) AS n FROM hands"))[0]["n"]

    async def list_hands(self, room):
        rows = await self.rows("SELECT id AS number,title FROM hands WHERE room=? ORDER BY id DESC LIMIT 30", room)
        return rows

    async def get_hand(self, room, number):
        rows = await self.rows("SELECT id AS number,events FROM hands WHERE room=? AND id=?", room, number)
        if not rows:
            return None
        row = rows[0]
        row["events"] = json.loads(row["events"])
        return row

    async def export_hands(self, room):
        rows = await self.rows("SELECT id,room,hand_number,played_at,title,events FROM hands WHERE room=? ORDER BY id", room)
        for row in rows:
            row["events"] = json.loads(row["events"])
        return rows
