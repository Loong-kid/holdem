"""Hand journal API: validation and D1 storage for hands recorded by hand from live play.

Journals are named books (e.g. "doil") that anyone can read. Writing to a book needs its password,
sent as X-Journal-Pin; only a salted, stretched hash of it is stored, and repeated misses lock the
book for a while. This module has no Workers imports so tests run it against sqlite.
"""
import hashlib
import hmac
import json
import math
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

NAME_RE = re.compile(r"^[0-9a-z가-힣_-]{2,20}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CARD_RE = re.compile(r"^[2-9TJQKA][shdc]$")
POSITIONS = {
    2: ["SB", "BB"],
    3: ["SB", "BB", "BTN"],
    4: ["SB", "BB", "UTG", "BTN"],
    5: ["SB", "BB", "UTG", "CO", "BTN"],
    6: ["SB", "BB", "UTG", "HJ", "CO", "BTN"],
    7: ["SB", "BB", "UTG", "UTG+1", "HJ", "CO", "BTN"],
    8: ["SB", "BB", "UTG", "UTG+1", "LJ", "HJ", "CO", "BTN"],
    9: ["SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"],
}
ACTS = {"fold", "check", "call", "bet", "raise", "allin"}
STREETS = ("pre", "flop", "turn", "river")
MAX_BODY = 32_000
MAX_HANDS = 5000
MAX_AMOUNT = 1e9
MAX_BOOKS = 2000
PIN_MIN, PIN_MAX = 4, 32
LOCK_AFTER = 8          # wrong passwords in a row before the book locks
LOCK_MINUTES = 10
STRETCH = 2000


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def book_name(raw):
    """Normalize a book name from a URL segment or a form field; raises ApiError(400)."""
    name = unicodedata.normalize("NFC", unquote(raw or "")).strip().lower()
    if not NAME_RE.match(name):
        raise ApiError(400, "기록장 이름은 한글·영문 소문자·숫자·-·_ 로 2~20자입니다")
    return name


def check_pin_format(pin):
    if not isinstance(pin, str) or not PIN_MIN <= len(pin) <= PIN_MAX or any(ord(c) < 32 for c in pin):
        raise ApiError(400, f"비밀번호는 {PIN_MIN}~{PIN_MAX}자입니다")
    return pin


def pin_hash(pin, salt):
    digest = (salt + ":" + pin).encode()
    for _ in range(STRETCH):
        digest = hashlib.sha256(digest + salt.encode()).digest()
    return digest.hex()


def _number(value, name, low, high, optional=False):
    if value is None or value == "":
        if optional:
            return None
        raise ValueError(f"{name} 값이 없습니다")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 값이 숫자가 아닙니다")
    if not low <= value <= high:
        raise ValueError(f"{name} 값이 범위를 벗어났습니다")
    return int(value) if float(value).is_integer() else round(float(value), 2)


def _text(value, name, limit):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} 형식이 잘못됐습니다")
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"{name}이(가) 너무 길거나 잘못된 문자가 있습니다")
    return value


def _cards(value, name, sizes):
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) not in sizes:
        raise ValueError(f"{name} 카드 수가 잘못됐습니다")
    out = []
    for card in value:
        if card is None:
            out.append(None)
        elif isinstance(card, str) and CARD_RE.match(card):
            out.append(card)
        else:
            raise ValueError(f"{name} 카드 형식이 잘못됐습니다")
    return out


def validate_hand(data, hand_id):
    """Return a normalized copy of a journal hand, or raise ValueError with a Korean message."""
    if not isinstance(data, dict):
        raise ValueError("핸드 형식이 잘못됐습니다")
    if not ID_RE.match(hand_id or "") or data.get("id") != hand_id:
        raise ValueError("핸드 ID가 잘못됐습니다")
    played_at = data.get("played_at")
    if not isinstance(played_at, str) or not DATE_RE.match(played_at):
        raise ValueError("날짜 형식이 잘못됐습니다")
    try:
        datetime.strptime(played_at, "%Y-%m-%d")
    except ValueError:
        raise ValueError("날짜 형식이 잘못됐습니다") from None
    size = data.get("table_size")
    if size not in POSITIONS:
        raise ValueError("인원은 2~9명이어야 합니다")
    positions = POSITIONS[size]
    hand = {
        "v": 1, "id": hand_id, "played_at": played_at,
        "session": _text(data.get("session"), "세션 이름", 60),
        "table_size": size,
        "sb": _number(data.get("sb"), "스몰 블라인드", 0, MAX_AMOUNT),
        "bb": _number(data.get("bb"), "빅 블라인드", 0.01, MAX_AMOUNT),
    }
    hero = data.get("hero")
    if not isinstance(hero, dict) or hero.get("pos") not in positions:
        raise ValueError("내 포지션을 골라 주세요")
    hand["hero"] = {"pos": hero["pos"], "stack": _number(hero.get("stack"), "내 스택", 0, MAX_AMOUNT, True),
                    "cards": _cards(hero.get("cards"), "내 패", (2,))}
    villains = data.get("villains") or []
    if not isinstance(villains, list) or len(villains) > size - 1:
        raise ValueError("상대 수가 인원보다 많습니다")
    seen = {hero["pos"]}
    hand["villains"] = []
    for villain in villains:
        if not isinstance(villain, dict) or villain.get("pos") not in positions or villain["pos"] in seen:
            raise ValueError("상대 포지션이 비었거나 겹칩니다")
        seen.add(villain["pos"])
        hand["villains"].append({
            "pos": villain["pos"], "stack": _number(villain.get("stack"), "상대 스택", 0, MAX_AMOUNT, True),
            "cards": _cards(villain.get("cards"), "상대 패", (0, 2)),
            "note": _text(villain.get("note"), "상대 메모", 60)})
    board = _cards(data.get("board"), "보드", (0, 5))
    board += [None] * (5 - len(board))
    flop, turn, river = board[:3], board[3], board[4]
    if any(flop) and not all(flop):
        raise ValueError("플랍은 3장을 모두 넣어 주세요")
    if (turn and not all(flop)) or (river and not turn):
        raise ValueError("보드 카드는 플랍·턴·리버 순서로 넣어 주세요")
    hand["board"] = board
    known = [c for c in hand["hero"]["cards"] + board if c]
    for villain in hand["villains"]:
        known += [c for c in villain["cards"] if c]
    if len(known) != len(set(known)):
        raise ValueError("같은 카드가 두 번 들어갔습니다")
    actions = data.get("actions") or {}
    if not isinstance(actions, dict):
        raise ValueError("액션 형식이 잘못됐습니다")
    hand["actions"] = {}
    for street in STREETS:
        rows = actions.get(street) or []
        if not isinstance(rows, list) or len(rows) > 80:
            raise ValueError("액션이 너무 많습니다")
        clean = []
        for row in rows:
            if not isinstance(row, dict) or row.get("pos") not in seen or row.get("act") not in ACTS:
                raise ValueError("액션의 플레이어나 종류가 잘못됐습니다")
            item = {"pos": row["pos"], "act": row["act"]}
            if row["act"] in ("bet", "raise"):
                item["to"] = _number(row.get("to"), "베팅 금액", 0.01, MAX_AMOUNT)
            clean.append(item)
        hand["actions"][street] = clean
    hand["result"] = _number(data.get("result"), "결과", -MAX_AMOUNT, MAX_AMOUNT, True)
    tags = data.get("tags") or []
    if not isinstance(tags, list) or len(tags) > 12:
        raise ValueError("태그는 12개까지입니다")
    hand["tags"] = []
    for tag in tags:
        tag = _text(tag, "태그", 20)
        if tag and tag not in hand["tags"]:
            hand["tags"].append(tag)
    hand["note"] = _text(data.get("note"), "메모", 2000)
    created = data.get("created_at")
    hand["created_at"] = created if isinstance(created, str) and len(created) <= 40 else None
    return hand


def now_iso(offset_minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _python(value):
    return value.to_py() if hasattr(value, "to_py") else value


class JournalRepository:
    def __init__(self, database):
        self.database = database

    async def _rows(self, sql, *parameters):
        return _python(await self.database.prepare(sql).bind(*parameters).all())["results"]

    async def _run(self, sql, *parameters):
        await self.database.prepare(sql).bind(*parameters).run()

    BOOK_SQL = ("SELECT b.name AS name, b.created_at AS created_at, count(h.hand_id) AS hands, "
                "max(h.played_at) AS last_played, coalesce(max(h.updated_at), b.created_at) AS updated_at "
                "FROM journal_books b LEFT JOIN journal_hands h ON h.owner = b.name ")

    async def books(self):
        return await self._rows(self.BOOK_SQL + "GROUP BY b.name ORDER BY updated_at DESC LIMIT 500")

    async def book(self, name):
        rows = await self._rows(self.BOOK_SQL + "WHERE b.name=? GROUP BY b.name", name)
        return rows[0] if rows else None

    async def create_book(self, name, pin):
        if await self._rows("SELECT 1 AS x FROM journal_books WHERE name=?", name):
            raise ApiError(409, "이미 있는 기록장 이름입니다. 내 기록장이면 '들어가기'로 비밀번호를 넣어 주세요")
        count = (await self._rows("SELECT count(*) AS n FROM journal_books"))[0]["n"]
        if count >= MAX_BOOKS:
            raise ApiError(400, "기록장을 더 만들 수 없습니다")
        salt = secrets.token_hex(16)
        await self._run("INSERT INTO journal_books(name,pin_salt,pin_hash,created_at) VALUES(?,?,?,?)",
                        name, salt, pin_hash(pin, salt), now_iso())
        return await self.book(name)

    async def check_pin(self, name, pin):
        """Raise ApiError unless pin opens the book; counts misses and locks after LOCK_AFTER of them."""
        rows = await self._rows("SELECT pin_salt, pin_hash, fail_count, locked_until FROM journal_books WHERE name=?", name)
        if not rows:
            raise ApiError(404, "없는 기록장입니다")
        row, now = rows[0], now_iso()
        if row["locked_until"] and row["locked_until"] > now:
            raise ApiError(423, f"비밀번호를 여러 번 틀려 {LOCK_MINUTES}분 동안 잠겼습니다")
        if isinstance(pin, str) and hmac.compare_digest(pin_hash(pin, row["pin_salt"]), row["pin_hash"]):
            if row["fail_count"]:
                await self._run("UPDATE journal_books SET fail_count=0, locked_until=NULL WHERE name=?", name)
            return
        misses = row["fail_count"] + 1
        if misses >= LOCK_AFTER:
            await self._run("UPDATE journal_books SET fail_count=0, locked_until=? WHERE name=?", now_iso(LOCK_MINUTES), name)
            raise ApiError(423, f"비밀번호를 여러 번 틀려 {LOCK_MINUTES}분 동안 잠겼습니다")
        await self._run("UPDATE journal_books SET fail_count=? WHERE name=?", misses, name)
        raise ApiError(401, "비밀번호가 맞지 않습니다")

    async def list(self, owner):
        rows = await self._rows("SELECT data FROM journal_hands WHERE owner=? "
                                "ORDER BY played_at DESC, updated_at DESC LIMIT ?", owner, MAX_HANDS)
        return [json.loads(row["data"]) for row in rows]

    async def save(self, owner, hand):
        exists = await self._rows("SELECT 1 AS x FROM journal_hands WHERE owner=? AND hand_id=?", owner, hand["id"])
        if not exists:
            count = (await self._rows("SELECT count(*) AS n FROM journal_hands WHERE owner=?", owner))[0]["n"]
            if count >= MAX_HANDS:
                raise ValueError(f"한 기록장에는 {MAX_HANDS}핸드까지 저장할 수 있습니다")
        stamp = now_iso()
        hand = dict(hand, updated_at=stamp, created_at=hand.get("created_at") or stamp)
        payload = json.dumps(hand, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        await self.database.prepare(
            "INSERT INTO journal_hands(owner,hand_id,played_at,updated_at,data) VALUES(?,?,?,?,?) "
            "ON CONFLICT(owner,hand_id) DO UPDATE SET played_at=excluded.played_at, "
            "updated_at=excluded.updated_at, data=excluded.data"
        ).bind(owner, hand["id"], hand["played_at"], stamp, payload).run()
        return hand

    async def delete(self, owner, hand_id):
        await self.database.prepare("DELETE FROM journal_hands WHERE owner=? AND hand_id=?").bind(owner, hand_id).run()


def _json_body(body):
    if len(body.encode()) > MAX_BODY:
        raise ApiError(413, "요청이 너무 큽니다")
    try:
        return json.loads(body)
    except ValueError:
        raise ApiError(400, "요청 형식이 잘못됐습니다") from None


async def handle(method, path, pin, body, database):
    """Route one /journal/api request. Returns (status, json-able payload).

    GET  /journal/api/books                        every book with hand counts (public)
    POST /journal/api/books           {name, pin}  create a book
    GET  /journal/api/books/<name>/hands           one book's hands (public)
    POST /journal/api/books/<name>/login           check X-Journal-Pin
    PUT|DELETE /journal/api/books/<name>/hands/<id> write, needs X-Journal-Pin
    """
    repository = JournalRepository(database)
    pin = unquote(pin) if isinstance(pin, str) else None   # the page percent-encodes it so any text fits a header
    parts = path[len("/journal/api/"):].strip("/").split("/")
    try:
        if parts[0] != "books":
            raise ApiError(404, "Not found")
        if len(parts) == 1:
            if method == "GET":
                return 200, {"books": await repository.books()}
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            data = _json_body(body)
            if not isinstance(data, dict):
                raise ApiError(400, "요청 형식이 잘못됐습니다")
            name = book_name(data.get("name"))
            book = await repository.create_book(name, check_pin_format(data.get("pin")))
            return 201, {"ok": True, "book": book}
        name, rest = book_name(parts[1]), parts[2:]
        if rest == ["hands"] and method == "GET":
            book = await repository.book(name)
            if not book:
                raise ApiError(404, "없는 기록장입니다")
            return 200, {"book": book, "hands": await repository.list(name)}
        if rest == ["login"]:
            if method != "POST":
                raise ApiError(405, "Method not allowed")
            await repository.check_pin(name, pin)
            return 200, {"ok": True, "book": await repository.book(name)}
        if len(rest) != 2 or rest[0] != "hands":
            raise ApiError(404, "Not found")
        hand_id = rest[1]
        if not ID_RE.match(hand_id):
            raise ApiError(400, "핸드 ID가 잘못됐습니다")
        if method not in ("PUT", "DELETE"):
            raise ApiError(405, "Method not allowed")
        data = _json_body(body) if method == "PUT" else None
        await repository.check_pin(name, pin)
        if method == "DELETE":
            await repository.delete(name, hand_id)
            return 200, {"ok": True}
        try:
            hand = validate_hand(data, hand_id)
        except (ValueError, TypeError) as error:
            raise ApiError(400, str(error)) from None
        try:
            return 200, {"ok": True, "hand": await repository.save(name, hand)}
        except ValueError as error:
            raise ApiError(400, str(error)) from None
    except ApiError as error:
        return error.status, {"error": str(error)}
