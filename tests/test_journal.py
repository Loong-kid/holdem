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

    def test_book_names(self):
        for raw in (None, "", "a", "has space", "x" * 21, "doil!", "%20doil%20x"):
            with self.assertRaises(journal.ApiError):
                journal.book_name(raw)
        self.assertEqual(journal.book_name(" DoIl "), "doil")
        self.assertEqual(journal.book_name("%EB%8F%84%EC%9D%BC"), "도일")   # URL-encoded Korean
        self.assertEqual(journal.book_name("river_ace-7"), "river_ace-7")


BOOK = "/journal/api/books"


async def make_book(db, name="doil", pin="1234"):
    return await journal.handle("POST", BOOK, None, json.dumps({"name": name, "pin": pin}), db)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_books_are_public_and_writes_need_the_pin(self):
        db = SQLiteBinding()
        status, body = await make_book(db)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["book"]["name"], "doil")
        self.assertNotIn("1234", tuple(db.connection.execute("SELECT pin_salt, pin_hash FROM journal_books").fetchone()))
        self.assertEqual((await make_book(db, "DOIL", "9999"))[0], 409)        # name taken, case-insensitive

        hand_url = BOOK + "/doil/hands/hand0001"
        status, body = await journal.handle("PUT", hand_url, "1234", json.dumps(sample()), db)
        self.assertEqual(status, 200, body)
        created = body["hand"]["created_at"]
        self.assertEqual((await journal.handle("PUT", hand_url, "0000", json.dumps(sample()), db))[0], 401)
        self.assertEqual((await journal.handle("PUT", hand_url, None, json.dumps(sample()), db))[0], 401)

        # anyone reads, no pin needed
        status, body = await journal.handle("GET", BOOK + "/doil/hands", None, "", db)
        self.assertEqual([h["id"] for h in body["hands"]], ["hand0001"])
        self.assertEqual(body["book"]["hands"], 1)
        status, body = await journal.handle("GET", BOOK, None, "", db)
        self.assertEqual([(b["name"], b["hands"]) for b in body["books"]], [("doil", 1)])

        # update keeps created_at; another book with the same hand id stays separate
        status, body = await journal.handle("PUT", hand_url, "1234", json.dumps(sample(result=-40, created_at=created)), db)
        self.assertEqual((body["hand"]["result"], body["hand"]["created_at"]), (-40, created))
        await make_book(db, "도일", "한글비번")
        status, _ = await journal.handle("PUT", BOOK + "/%EB%8F%84%EC%9D%BC/hands/hand0001", "%ED%95%9C%EA%B8%80%EB%B9%84%EB%B2%88",
                                         json.dumps(sample()), db)
        self.assertEqual(status, 200)
        self.assertEqual(db.connection.execute("SELECT count(*) FROM journal_hands").fetchone()[0], 2)

        self.assertEqual((await journal.handle("DELETE", hand_url, "0000", "", db))[0], 401)
        self.assertEqual((await journal.handle("DELETE", hand_url, "1234", "", db))[0], 200)
        self.assertEqual(db.connection.execute("SELECT count(*) FROM journal_hands WHERE owner='doil'").fetchone()[0], 0)

    async def test_login_locks_after_repeated_misses(self):
        db = SQLiteBinding()
        await make_book(db)
        login = BOOK + "/doil/login"
        self.assertEqual((await journal.handle("POST", login, "1234", "", db))[0], 200)
        for _ in range(journal.LOCK_AFTER - 1):
            self.assertEqual((await journal.handle("POST", login, "nope", "", db))[0], 401)
        self.assertEqual((await journal.handle("POST", login, "nope", "", db))[0], 423)
        self.assertEqual((await journal.handle("POST", login, "1234", "", db))[0], 423)   # even the right pin waits
        db.connection.execute("UPDATE journal_books SET locked_until='2000-01-01T00:00:00Z'")
        self.assertEqual((await journal.handle("POST", login, "1234", "", db))[0], 200)
        self.assertEqual((await journal.handle("POST", BOOK + "/nobody/login", "1234", "", db))[0], 404)

    async def test_errors(self):
        db = SQLiteBinding()
        await make_book(db)
        hand_url = BOOK + "/doil/hands/hand0001"
        self.assertEqual((await make_book(db, "x", "1234"))[0], 400)
        self.assertEqual((await make_book(db, "okname", "12"))[0], 400)
        self.assertEqual((await journal.handle("POST", BOOK, None, "{not json", db))[0], 400)
        self.assertEqual((await journal.handle("PUT", hand_url, "1234", "{not json", db))[0], 400)
        self.assertEqual((await journal.handle("PUT", hand_url, "1234", json.dumps(sample(bb=0)), db))[0], 400)
        self.assertEqual((await journal.handle("PUT", BOOK + "/doil/hands/bad id", "1234", "{}", db))[0], 400)
        self.assertEqual((await journal.handle("POST", hand_url, "1234", "{}", db))[0], 405)
        self.assertEqual((await journal.handle("PUT", hand_url, "1234", "x" * 40000, db))[0], 413)
        self.assertEqual((await journal.handle("GET", BOOK + "/nobody/hands", None, "", db))[0], 404)
        self.assertEqual((await journal.handle("GET", "/journal/api/hands", None, "", db))[0], 404)


if __name__ == "__main__":
    unittest.main()
