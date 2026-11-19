"""Hand journal API: validation and D1 storage for hands recorded by hand from live play.

A personal code (sent as X-Journal-Code) identifies the journal; only its salted SHA-256 is stored.
This module has no Workers imports so tests run it against sqlite.
"""
import hashlib
import json
import math
import re
from datetime import datetime, timezone

CODE_RE = re.compile(r"^[a-z0-9-]{6,64}$")
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


class AuthError(Exception):
    pass


def owner_key(code):
    code = (code or "").strip().lower()
    if not CODE_RE.match(code):
        raise AuthError("기록 코드가 필요합니다 (영문 소문자·숫자·- 6~64자)")
    return hashlib.sha256(("holdem-journal:v1:" + code).encode()).hexdigest()


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


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _python(value):
    return value.to_py() if hasattr(value, "to_py") else value


class JournalRepository:
    def __init__(self, database):
        self.database = database

    async def _rows(self, sql, *parameters):
        return _python(await self.database.prepare(sql).bind(*parameters).all())["results"]

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


async def handle(method, path, code, body, database):
    """Route one /journal/api request. Returns (status, json-able payload)."""
    try:
        owner = owner_key(code)
    except AuthError as error:
        return 401, {"error": str(error)}
    repository = JournalRepository(database)
    if path == "/journal/api/hands" and method == "GET":
        return 200, {"hands": await repository.list(owner)}
    prefix = "/journal/api/hands/"
    if not path.startswith(prefix):
        return 404, {"error": "Not found"}
    hand_id = path[len(prefix):]
    if not ID_RE.match(hand_id):
        return 400, {"error": "핸드 ID가 잘못됐습니다"}
    if method == "DELETE":
        await repository.delete(owner, hand_id)
        return 200, {"ok": True}
    if method != "PUT":
        return 405, {"error": "Method not allowed"}
    if len(body.encode()) > MAX_BODY:
        return 413, {"error": "핸드가 너무 큽니다"}
    try:
        hand = validate_hand(json.loads(body), hand_id)
        return 200, {"ok": True, "hand": await repository.save(owner, hand)}
    except (ValueError, TypeError) as error:
        return 400, {"error": str(error)}
