import json
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cloudflare"))
import journal  # noqa: E402


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
        for migration in sorted((ROOT / "cloudflare/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text(encoding="utf-8"))

    def prepare(self, sql):
        return Statement(self.connection, sql)


def sample(hand_id="hand0001", **changes):
    hand = {
        "id": hand_id, "played_at": "2026-11-20", "session": "금요일 홈게임", "table_size": 6, "sb": 1, "bb": 2,
        "hero": {"pos": "BTN", "stack": 400, "cards": ["As", "Kd"]},
        "villains": [{"pos": "BB", "stack": 250, "cards": ["Qh", "Qc"], "note": "타이트"}],
        "board": ["Kh", "7c", "2d", "9s", None],
        "actions": {"pre": [{"pos": "BTN", "act": "raise", "to": 6}, {"pos": "BB", "act": "call"}],
                    "flop": [{"pos": "BB", "act": "check"}, {"pos": "BTN", "act": "bet", "to": 4.5}, {"pos": "BB", "act": "call"}]},
        "result": 21, "tags": ["밸류", "밸류", "3벳팟"], "note": "턴 체크백", "extra": "dropped",
    }
    hand.update(changes)
    return hand


class ValidationTests(unittest.TestCase):
    def test_normalizes_and_drops_unknown_fields(self):
        hand = journal.validate_hand(sample(), "hand0001")
        self.assertNotIn("extra", hand)
        self.assertEqual(hand["tags"], ["밸류", "3벳팟"])
        self.assertEqual(hand["board"], ["Kh", "7c", "2d", "9s", None])
        self.assertEqual(hand["actions"]["turn"], [])
        self.assertEqual(hand["actions"]["flop"][1], {"pos": "BTN", "act": "bet", "to": 4.5})
        self.assertNotIn("to", hand["actions"]["pre"][1])

    def test_rejects_bad_hands(self):
        bad = [
            sample(hero={"pos": "BTN", "stack": 400, "cards": ["As", "As"]}),           # duplicate card
            sample(board=["Kh", "7c", None, "9s", None]),                                 # partial flop
            sample(board=[None, None, None, "9s", None]),                                 # turn without flop
            sample(hero={"pos": "XX", "stack": 1, "cards": [None, None]}),                # unknown position
            sample(villains=[{"pos": "BTN"}]),                                            # same seat as hero
            sample(actions={"pre": [{"pos": "CO", "act": "call"}]}),                      # actor not in hand
            sample(actions={"pre": [{"pos": "BTN", "act": "raise"}]}),                    # raise without amount
            sample(table_size=10),
            sample(played_at="2026-02-30"),
            sample(bb=0),
            sample(result=float("nan")),
            sample(tags=["x"] * 13),
        ]
        for hand in bad:
            with self.assertRaises(ValueError):
                journal.validate_hand(hand, "hand0001")
        with self.assertRaises(ValueError):
            journal.validate_hand(sample(), "other-id")

    def test_owner_key_needs_a_real_code(self):
        for code in (None, "", "abc", "has space", "대문자"):
            with self.assertRaises(journal.AuthError):
                journal.owner_key(code)
        self.assertEqual(journal.owner_key(" River-Ace-7k "), journal.owner_key("river-ace-7k"))
        self.assertNotIn("river", journal.owner_key("river-ace-7k"))


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_is_scoped_by_code(self):
        db = SQLiteBinding()
        status, body = await journal.handle("PUT", "/journal/api/hands/hand0001", "river-ace-7k", json.dumps(sample()), db)
        self.assertEqual(status, 200, body)
        created = body["hand"]["created_at"]
        status, body = await journal.handle("GET", "/journal/api/hands", "river-ace-7k", "", db)
        self.assertEqual([h["id"] for h in body["hands"]], ["hand0001"])
        self.assertEqual(body["hands"][0]["hero"]["cards"], ["As", "Kd"])
        status, other = await journal.handle("GET", "/journal/api/hands", "someone-else", "", db)
        self.assertEqual(other["hands"], [])
        # update keeps created_at, replaces the content
        status, body = await journal.handle("PUT", "/journal/api/hands/hand0001", "river-ace-7k",
                                            json.dumps(sample(result=-40, created_at=created)), db)
        self.assertEqual(body["hand"]["result"], -40)
        self.assertEqual(body["hand"]["created_at"], created)
        rows = db.connection.execute("SELECT count(*) FROM journal_hands").fetchone()[0]
        self.assertEqual(rows, 1)
        status, _ = await journal.handle("DELETE", "/journal/api/hands/hand0001", "someone-else", "", db)
        self.assertEqual(db.connection.execute("SELECT count(*) FROM journal_hands").fetchone()[0], 1)
        status, _ = await journal.handle("DELETE", "/journal/api/hands/hand0001", "river-ace-7k", "", db)
        self.assertEqual(db.connection.execute("SELECT count(*) FROM journal_hands").fetchone()[0], 0)

    async def test_errors(self):
        db = SQLiteBinding()
        self.assertEqual((await journal.handle("GET", "/journal/api/hands", None, "", db))[0], 401)
        self.assertEqual((await journal.handle("PUT", "/journal/api/hands/hand0001", "river-ace-7k", "{not json", db))[0], 400)
        self.assertEqual((await journal.handle("PUT", "/journal/api/hands/bad id", "river-ace-7k", "{}", db))[0], 400)
        self.assertEqual((await journal.handle("POST", "/journal/api/hands/hand0001", "river-ace-7k", "{}", db))[0], 405)
        self.assertEqual((await journal.handle("PUT", "/journal/api/hands/hand0001", "river-ace-7k", "x" * 40000, db))[0], 413)
        self.assertEqual((await journal.handle("GET", "/journal/api/other", "river-ace-7k", "", db))[0], 404)


if __name__ == "__main__":
    unittest.main()
