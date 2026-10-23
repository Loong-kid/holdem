import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cloudflare.repository import HandRepository
from tools.import_legacy import normalized_rows, import_sql


class Statement:
    def __init__(self, database, sql):
        self.database, self.sql, self.values = database, sql, ()

    def bind(self, *values):
        self.values = values
        return self

    async def run(self):
        self.database.execute(self.sql, self.values)
        return {"success": True}

    async def all(self):
        return {"results": [dict(r) for r in self.database.execute(self.sql, self.values)]}


class SQLiteBinding:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript((ROOT / "cloudflare/migrations/0001_hands.sql").read_text())

    def prepare(self, sql):
        return Statement(self.connection, sql)


class D1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_idempotence_collision_and_room_isolation(self):
        binding = SQLiteBinding()
        self.addCleanup(binding.connection.close)
        repository = HandRepository(binding)
        hand = {"session_id": "s1", "room": "A'--", "hand_number": 1,
                "played_at": "2026-10-23T00:00:00Z", "title": "Hand", "events": [{"type": "test"}]}
        await repository.save(hand)
        await repository.save(hand)
        self.assertEqual(await repository.count(), 1)
        self.assertEqual(await repository.list_hands("B"), [])
        self.assertIsNone(await repository.get_hand("B", 1))
        self.assertEqual((await repository.get_hand("A'--", 1))["events"], hand["events"])
        conflicting = copy.deepcopy(hand)
        conflicting["events"] = []
        with self.assertRaises(ValueError):
            await repository.save(conflicting)
        self.assertEqual((await repository.export_hands("A'--"))[0]["events"], hand["events"])
        hand["session_id"] = "s2"  # Same room, hand numbering restarts.
        await repository.save(hand)
        self.assertEqual(await repository.count(), 2)

    async def test_legacy_import_repeated_original_numbers_and_sql_quotes(self):
        binding = SQLiteBinding()
        self.addCleanup(binding.connection.close)
        raw = [{"id": i, "room": "Korean ' room", "hand_number": 1,
                "played_at": "2026-10-23T00:00:00Z", "title": "A's hand",
                "events": [{"name": "O'Brien", "text": "');DROP TABLE hands;--"}]} for i in (1, 2)]
        rows = list(normalized_rows({"hands": raw}))
        sql = import_sql(rows)
        binding.connection.executescript(sql)
        binding.connection.executescript(sql)
        repo = HandRepository(binding)
        self.assertEqual(await repo.count(), 2)
        self.assertEqual((await repo.export_hands("Korean ' room"))[0]["events"], raw[0]["events"])


if __name__ == "__main__":
    unittest.main()
