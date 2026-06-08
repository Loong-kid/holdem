"""FastAPI server that puts the poker engine online.

Architecture in one picture:

    browser  <--WebSocket-->  Room (this file)  -->  Game (poker/game.py)
                                  |
                              broadcasts state back to every browser in the room

Each browser opens one WebSocket. When a player does something, the message comes
in here, we ask the Game engine to apply it, then we push the new state out to
*everyone* at that table. Each player additionally receives a private slice with
their own hole cards, so nobody can peek at someone else's hand.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import db
from poker.game import Game, summarize_hand

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

NEXT_HAND_DELAY = 5.0      # seconds to show results before auto-dealing the next hand
RUNOUT_DELAY = 1.3         # seconds between board reveals on an all-in run-out
DEFAULT_TIMEOUT = 30       # seconds per action
MIN_TIMEOUT, MAX_TIMEOUT = 20, 60
DISCONNECT_GRACE = 60      # seconds a dropped player keeps their seat to reconnect
APP_VERSION = "v24-export"   # bump on deploy so we can confirm what's live

# ---- Tournament defaults --------------------------------------------------
# A blind level is just (small_blind, big_blind). The clock auto-advances to the
# next level every `level_minutes`; the new blinds take effect on the next hand.
DEFAULT_TOURNAMENT_LEVELS = [
    (10, 20), (15, 30), (20, 40), (25, 50), (50, 100),
    (75, 150), (100, 200), (150, 300), (200, 400), (300, 600),
    (400, 800), (500, 1000), (700, 1400), (1000, 2000), (1500, 3000),
    (2000, 4000), (3000, 6000), (4000, 8000), (5000, 10000), (7500, 15000),
]
DEFAULT_LEVEL_MINUTES = 15
MIN_LEVEL_MINUTES, MAX_LEVEL_MINUTES = 10, 40
MAX_LEVELS = 20


@asynccontextmanager
async def lifespan(app: FastAPI):
    on = await db.init()
    print("Hand persistence:", "PostgreSQL" if on else "in-memory only", flush=True)
    yield
    await db.close()


app = FastAPI(lifespan=lifespan)


class Room:
    """One poker table: a Game plus the set of connected browsers."""

    def __init__(self, room_id: str):
        self.id = room_id
        self.game = Game(small_blind=5, big_blind=10, starting_chips=1000)
        # websocket -> seated player id, or None for a spectator (watching only).
        self.connections: dict[WebSocket, str | None] = {}
        self.conn_name: dict[WebSocket, str] = {}   # ws -> display name (sit / chat)
        # Access control for taking a seat:
        #   tokens     = per-browser token -> seated pid (same browser resumes its seat)
        #   player_ip  = seated pid -> client IP (one seat per IP unless allowed)
        self.tokens: dict[str, str] = {}
        self.player_ip: dict[str, str] = {}
        self.allow_same_ip = False        # host can allow multiple seats per IP
        self.host_id: str | None = None   # first SEATED player owns the host controls
        self.lock = asyncio.Lock()   # serialize actions so the engine sees one at a time
        # Cash-game ledger keyed by nickname. Survives a player leaving so the
        # leaderboard keeps their record. Tracks money in/out of the table.
        #   buyin   = chips bought in with on first sit
        #   added   = chips topped up later (host action)
        #   removed = chips taken off the table (host action)
        #   last_stack = most recent chip count (live, or frozen when they left)
        #   active  = is someone currently connected under this name
        self.ledger: dict[str, dict] = {}
        self.chat: list[dict] = []         # recent chat messages {name, text}
        # Players whose connection dropped but who keep their seat + cards for a
        # grace period so a brief network blip doesn't fold them out.
        #   pid -> monotonic deadline to actually drop them
        self.disconnected: dict[str, float] = {}

        # ---- auto-deal + action timer ----
        self.auto_running = False          # is the table continuously dealing?
        self.timeout_seconds = DEFAULT_TIMEOUT
        self.action_deadline: float | None = None   # monotonic time the current actor must act by
        self.action_remaining: float | None = None  # frozen seconds left while paused
        self.next_hand_at: float | None = None      # monotonic time to deal the next hand
        self.runout_at: float | None = None          # monotonic time to reveal the next street
        self.loop_task: asyncio.Task | None = None   # background ticker
        self.persisted_count = 0                     # hands already written to the DB

        # ---- tournament (auto-rising blinds) ----
        # When enabled, blinds follow `blind_levels` and advance one level every
        # `level_minutes`. The clock only runs while the table is auto_running, so
        # pausing the game pauses the blind clock too.
        self.tournament = False
        self.level_minutes = DEFAULT_LEVEL_MINUTES
        self.blind_levels: list[list[int]] = [list(lv) for lv in DEFAULT_TOURNAMENT_LEVELS]
        self.level_index = 0                         # 0-based index into blind_levels
        self.level_deadline: float | None = None     # monotonic time the level ends (running)
        self.level_remaining: float | None = None    # seconds left when paused
        self.tourney_active = False                  # clock has started and not been reset

    async def persist_new_hands(self):
        """Write any newly-finished hands to the database (no-op without a DB)."""
        if not db.enabled():
            return
        while self.persisted_count < self.game.hand_count:
            self.persisted_count += 1
            n = self.persisted_count
            rec = next((r for r in self.game.hand_log if r["number"] == n), None)
            if rec:
                try:
                    await db.save_hand(self.id, n, summarize_hand(rec), rec["events"])
                except Exception as e:
                    print("save_hand failed:", e)

    # ---- timing engine --------------------------------------------------------

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def arm_timer(self):
        """(Re)start the action clock for whoever is currently to act."""
        self.action_remaining = None
        g = self.game
        if g.hand_in_progress and g.to_act is not None:
            self.action_deadline = self._now() + self.timeout_seconds
        else:
            self.action_deadline = None

    def pause_action_timer(self):
        """Freeze the current actor's clock so a pause really stops the time."""
        if self.action_deadline is not None:
            self.action_remaining = max(0.0, self.action_deadline - self._now())
            self.action_deadline = None

    def resume_action_timer(self):
        """Restore the frozen action clock when the table resumes."""
        g = self.game
        if (g.hand_in_progress and g.to_act is not None
                and self.action_remaining is not None):
            self.action_deadline = self._now() + self.action_remaining
        self.action_remaining = None

    def ensure_loop(self):
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self._run_loop())

    def stop_loop(self):
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
        self.loop_task = None
        self.auto_running = False
        self.action_deadline = None
        self.action_remaining = None
        self.next_hand_at = None
        self.runout_at = None

    # ---- disconnect / reconnect ----------------------------------------------

    def mark_disconnected(self, pid: str):
        """Connection dropped: keep the seat + cards, start the grace countdown."""
        p = self.game._player(pid)
        if not p:
            return
        if p.name in self.ledger:
            self.ledger[p.name]["active"] = False
            self.ledger[p.name]["last_stack"] = p.chips
        self.disconnected[pid] = self._now() + DISCONNECT_GRACE
        self.game.log.append(f"{p.name} 연결 끊김 (재접속 대기).")

    def drop_player(self, pid: str):
        """Remove a player for good (intentional leave/stand, or grace expired)."""
        self.disconnected.pop(pid, None)
        self.player_ip.pop(pid, None)
        for tk in [k for k, v in self.tokens.items() if v == pid]:
            self.tokens.pop(tk, None)
        gone = self.game._player(pid)
        if gone and gone.name in self.ledger:
            self.ledger[gone.name]["last_stack"] = gone.chips
            self.ledger[gone.name]["active"] = False
        self.game.remove_player(pid)
        if self.host_id == pid:   # host left -> pass the crown to a remaining seat
            self.host_id = self._first_seated()

    def reconnected_pid(self, name: str) -> str | None:
        """If a disconnected player with this nickname is still seated, their id."""
        for pid in self.disconnected:
            p = self.game._player(pid)
            if p and p.name == name:
                return pid
        return None

    # ---- seating / access control --------------------------------------------

    def _first_seated(self) -> str | None:
        for p in self.game.players:
            if not p.pending_removal:
                return p.id
        return None

    def pid_for_token(self, token: str | None) -> str | None:
        """The seat this browser already owns (same-browser auto-resume)."""
        if not token:
            return None
        pid = self.tokens.get(token)
        return pid if (pid and self.game._player(pid)) else None

    def pid_for_ip(self, ip: str) -> str | None:
        """An existing seat from this IP, if any (one seat per IP enforcement)."""
        for p_id, p_ip in self.player_ip.items():
            if p_ip == ip and self.game._player(p_id):
                return p_id
        return None

    def conn_display_name(self, ws) -> str:
        pid = self.connections.get(ws)
        if pid:
            p = self.game._player(pid)
            if p:
                return p.name
        return self.conn_name.get(ws, "관전자")

    # ---- tournament blind clock ----------------------------------------------

    def apply_level_blinds(self):
        """Push the current level's blinds into the game engine (next hand)."""
        i = max(0, min(self.level_index, len(self.blind_levels) - 1))
        sb, bb = self.blind_levels[i]
        self.game.set_blinds(sb, bb)
        self.game.log.append(f"🏆 블라인드 레벨 {i + 1}: {sb}/{bb}")

    def start_tournament_clock(self):
        """Begin level 1 from scratch and start its timer."""
        self.level_index = 0
        self.apply_level_blinds()
        self.level_deadline = self._now() + self.level_minutes * 60
        self.level_remaining = None
        self.tourney_active = True

    def resume_tournament_clock(self):
        """Called when the table resumes (start pressed). Starts or un-pauses."""
        if not self.tourney_active:
            self.start_tournament_clock()
        elif self.level_remaining is not None:        # un-pause: count from what was left
            self.level_deadline = self._now() + self.level_remaining
            self.level_remaining = None
        # else: at the final level (no deadline, nothing to resume)

    def pause_tournament_clock(self):
        """Called when the table pauses. Freeze the remaining time."""
        if self.level_deadline is not None:
            self.level_remaining = max(0.0, self.level_deadline - self._now())
            self.level_deadline = None

    def reset_tournament_clock(self):
        self.level_index = 0
        self.level_deadline = None
        self.level_remaining = None
        self.tourney_active = False

    def tournament_state(self) -> dict:
        """Tournament info for the wire (drives the top-bar clock + settings editor)."""
        if not self.tournament:
            return {"enabled": False, "levels": self.blind_levels,
                    "minutes": self.level_minutes}
        if self.level_deadline is not None:
            time_left = max(0.0, self.level_deadline - self._now())
            running = True
        elif self.level_remaining is not None:
            time_left = self.level_remaining
            running = False
        else:
            time_left = None                         # not started, or final level
            running = False
        i = min(self.level_index, len(self.blind_levels) - 1)
        sb, bb = self.blind_levels[i]
        return {
            "enabled": True,
            "level": i + 1,
            "total_levels": len(self.blind_levels),
            "minutes": self.level_minutes,
            "sb": sb, "bb": bb,
            "is_last": i >= len(self.blind_levels) - 1,
            "running": running,
            "time_left": time_left,
            "levels": self.blind_levels,
        }

    def _auto_act(self):
        """Time ran out: act for the player automatically (check, else fold)."""
        g = self.game
        if not g.hand_in_progress or g.to_act is None:
            return
        if not (0 <= g.to_act < len(g.players)):   # stale index guard (defensive)
            return
        pid = g.players[g.to_act].id
        legal = g.legal_actions(pid)
        if legal and legal.get("can_check"):
            g.act(pid, "check")
        else:
            g.act(pid, "fold")

    def _tick(self) -> bool:
        """One step of the background clock. Returns True if state changed."""
        g = self.game
        now = self._now()
        changed = False

        # Drop players whose reconnect grace period has run out.
        if self.disconnected:
            for pid in [k for k, dl in self.disconnected.items() if now >= dl]:
                self.drop_player(pid)
                changed = True

        # Tournament blind clock: independent of whose turn it is. Advancing a
        # level just changes the blinds; they apply on the next hand (engine
        # rule), so it is safe to fire even mid-hand.
        if self.tournament and self.auto_running and self.level_deadline is not None:
            if now >= self.level_deadline:
                if self.level_index < len(self.blind_levels) - 1:
                    self.level_index += 1
                    self.apply_level_blinds()
                    self.level_deadline = now + self.level_minutes * 60
                else:
                    self.level_deadline = None     # reached the final level; stop rising
                changed = True

        if g.hand_in_progress and g.to_act is not None:
            self.next_hand_at = None
            self.runout_at = None
            if self.action_deadline is not None and now >= self.action_deadline:
                self._auto_act()
                self.arm_timer()     # arm for the next actor (or clear if hand ended)
                return True
            return changed

        # All-in run-out: reveal the next board street after a short delay so the
        # flop/turn/river are watchable instead of flashing by at once.
        if g.hand_in_progress and g.awaiting_runout:
            self.action_deadline = None
            self.next_hand_at = None
            if not self.auto_running:          # paused -> hold the run-out too
                return changed
            if self.runout_at is None:
                self.runout_at = now + RUNOUT_DELAY
            elif now >= self.runout_at:
                g.deal_next_runout()
                self.runout_at = None
                self.arm_timer()
                return True
            return changed
        self.runout_at = None

        # Between hands: auto-deal the next one if the table is running.
        self.action_deadline = None
        if self.auto_running and g.can_start():
            if self.next_hand_at is None:
                self.next_hand_at = now + NEXT_HAND_DELAY
            elif now >= self.next_hand_at:
                g.start_hand()
                self.next_hand_at = None
                self.arm_timer()
                return True
        else:
            self.next_hand_at = None
        return changed

    async def _run_loop(self):
        try:
            while True:
                await asyncio.sleep(0.25)
                try:
                    async with self.lock:
                        changed = self._tick()
                        idle = not self.connections and not self.disconnected
                    if changed:
                        await self.broadcast()
                        await self.persist_new_hands()
                    if idle:    # nobody here and nobody to wait for -> let the loop end
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # One bad tick must NOT kill the ticker (that would freeze the
                    # whole table). Log and keep going; the next tick re-broadcasts.
                    print("run-loop tick error:", repr(e), flush=True)
        except asyncio.CancelledError:
            pass

    def ledger_entry(self, name: str) -> dict:
        return self.ledger.setdefault(
            name, {"buyin": 0, "added": 0, "removed": 0,
                   "last_stack": 0, "active": False})

    def ledger_view(self) -> list[dict]:
        """Snapshot the ledger for the wire, refreshing live stacks and net."""
        live = {p.name: p.chips for p in self.game.players}
        rows = []
        for name, e in self.ledger.items():
            if name in live:
                e["last_stack"] = live[name]
            net = e["last_stack"] + e["removed"] - e["buyin"] - e["added"]
            rows.append({"name": name, "buyin": e["buyin"], "added": e["added"],
                         "removed": e["removed"], "stack": e["last_stack"],
                         "net": net, "active": e["active"]})
        rows.sort(key=lambda r: r["net"], reverse=True)
        return rows

    async def broadcast(self):
        """Send the latest state to every connected player (public + their private)."""
        public = self.game.public_state()
        public["host"] = self.host_id
        dc = set(self.disconnected.keys())
        for pl in public["players"]:
            pl["disconnected"] = pl["id"] in dc
        public["ledger"] = self.ledger_view()
        public["auto_running"] = self.auto_running
        public["action_timeout"] = self.timeout_seconds
        public["chat"] = self.chat[-60:]
        public["db"] = db.enabled()
        public["version"] = APP_VERSION
        public["tournament"] = self.tournament_state()
        public["allow_same_ip"] = self.allow_same_ip
        public["spectators"] = sum(1 for v in self.connections.values() if v is None)
        # Seconds left for the current actor (clients run their own countdown from
        # this). While paused we send the frozen remaining so it shows but doesn't tick.
        time_left = None
        if self.game.hand_in_progress and self.game.to_act is not None:
            if self.action_deadline is not None:
                time_left = max(0.0, self.action_deadline - self._now())
            elif self.action_remaining is not None:
                time_left = self.action_remaining
        public["time_left"] = time_left
        dead = []
        # Iterate a SNAPSHOT: send_json awaits, and a concurrent join/leave/
        # reconnect can mutate self.connections during that await. Iterating the
        # live dict would raise "dictionary changed size during iteration" and
        # kill the broadcast (and, in the run loop, freeze the whole table).
        for ws, pid in list(self.connections.items()):
            payload = {
                "type": "state",
                "public": public,
                "private": self.game.private_state(pid),
            }
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            pid = self.connections.pop(ws, None)
            # A failed send means the socket is gone: start their grace period so
            # they can reconnect instead of being silently stuck.
            if pid and pid not in self.disconnected and self.game._player(pid):
                self.mark_disconnected(pid)


class RoomManager:
    def __init__(self):
        self.rooms: dict[str, Room] = {}

    def get(self, room_id: str) -> Room:
        if room_id not in self.rooms:
            self.rooms[room_id] = Room(room_id)
        return self.rooms[room_id]


manager = RoomManager()


def client_ip(ws: WebSocket) -> str:
    """Real client IP, honouring the proxy header (Render sits in front of us)."""
    xff = ws.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return ws.client.host if ws.client else "?"


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    """Quick check of whether replay persistence is wired to a database."""
    return {**db.status(), "version": APP_VERSION}


@app.get("/export")
async def export(room: str = "main"):
    """Download every stored hand for a room as one JSON file (for analysis).

    Pulls from the database when configured; otherwise serves the in-memory
    hand log of the live room. The file is the full event stream per hand, so it
    has the same fidelity as the replay viewer.
    """
    if db.enabled():
        hands = await db.export_hands(room)
    else:
        r = manager.rooms.get(room)
        hands = []
        if r:
            for rec in r.game.hand_log:
                hands.append({
                    "id": rec["number"], "room": room,
                    "hand_number": rec["number"], "played_at": None,
                    "title": summarize_hand(rec), "events": rec["events"],
                })
    payload = {"room": room, "exported_at": _now_iso(),
               "count": len(hands), "hands": hands}
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"holdem_{room}_{len(hands)}hands.json"
    return Response(
        content=body, media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    room: Room | None = None
    pid: str | None = None
    left_clean = False        # set when the player clicks "나가기" (vs a network drop)
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "join":
                # Joining a room = watching as a SPECTATOR. You only get a seat
                # (cards/chips) after pressing "테이블에 앉기" (the "sit" message).
                # Exception: if this browser already owns a seat (token) or is a
                # dropped player within the grace window, we resume that seat.
                room_id = (msg.get("room") or "main").strip() or "main"
                name = (msg.get("name") or "Player").strip()[:16] or "Player"
                token = (msg.get("token") or "").strip() or None
                ip = client_ip(ws)
                room = manager.get(room_id)
                async with room.lock:
                    room.conn_name[ws] = name
                    bound = room.pid_for_token(token) or room.reconnected_pid(name)
                    if bound is not None:
                        pid = bound                     # resume the existing seat
                        room.disconnected.pop(pid, None)
                        if token:
                            room.tokens[token] = pid
                        room.player_ip[pid] = ip
                        p = room.game._player(pid)
                        if p and p.name in room.ledger:
                            room.ledger[p.name]["active"] = True
                        room.connections[ws] = pid
                    else:
                        pid = None                      # spectator
                        room.connections[ws] = None
                    seated_name = room.game._player(pid).name if pid else name
                await ws.send_json({"type": "joined", "id": pid, "room": room_id,
                                    "name": seated_name, "seated": pid is not None})
                room.ensure_loop()      # make sure the timer/auto-deal clock is running
                await room.broadcast()

            elif mtype == "sit" and room and pid is None:
                # Spectator takes a seat. IP / nickname / capacity checks happen here.
                name = (msg.get("name") or room.conn_name.get(ws) or "Player").strip()[:16] or "Player"
                token = (msg.get("token") or "").strip() or None
                ip = client_ip(ws)
                candidate = uuid.uuid4().hex[:8]
                error = None
                async with room.lock:
                    room.conn_name[ws] = name
                    entry = room.ledger.get(name)
                    if (not room.allow_same_ip) and room.pid_for_ip(ip) is not None:
                        error = ("같은 네트워크(IP)에서 이미 플레이 중입니다. 같은 와이파이에서 "
                                 "함께 치려면 방장이 설정에서 '같은 IP 허용'을 켜주세요.")
                    elif entry and entry["active"]:
                        error = "이미 사용 중인 닉네임입니다. 다른 이름을 써주세요."
                    elif room.game.is_full():
                        error = "테이블이 가득 찼습니다 (최대 9명)."
                    else:
                        if entry is not None:
                            room.game.add_player(candidate, name, entry["last_stack"])
                            entry["active"] = True
                        else:
                            start = room.game.starting_chips
                            room.game.add_player(candidate, name, start)
                            room.ledger[name] = {"buyin": start, "added": 0, "removed": 0,
                                                 "last_stack": start, "active": True}
                        pid = candidate
                        room.connections[ws] = pid
                        if token:
                            room.tokens[token] = pid
                        room.player_ip[pid] = ip
                        if room.host_id is None:        # first seated player hosts
                            room.host_id = pid
                if error is not None:
                    await ws.send_json({"type": "error", "message": error})
                else:
                    await ws.send_json({"type": "seated", "id": pid, "name": name})
                    await room.broadcast()

            elif mtype == "stand" and room and pid:
                # Leave the seat and go back to watching.
                async with room.lock:
                    room.drop_player(pid)
                    room.connections[ws] = None
                    pid = None
                await ws.send_json({"type": "stood"})
                await room.broadcast()

            elif mtype == "set_allow_same_ip" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 설정을 바꿀 수 있습니다."})
                else:
                    async with room.lock:
                        room.allow_same_ip = bool(msg.get("value"))
                    await room.broadcast()

            elif mtype == "start" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error",
                                        "message": "방장만 게임을 시작할 수 있습니다."})
                else:
                    async with room.lock:
                        room.auto_running = True       # keep dealing hands automatically
                        room.next_hand_at = None
                        if room.tournament:
                            room.resume_tournament_clock()
                        if not room.game.hand_in_progress and room.game.can_start():
                            room.game.start_hand()
                            room.arm_timer()
                        else:
                            room.resume_action_timer()  # un-freeze a paused mid-hand
                    await room.broadcast()

            elif mtype == "pause" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error",
                                        "message": "방장만 게임을 멈출 수 있습니다."})
                else:
                    async with room.lock:
                        room.auto_running = False      # pause: freeze the hand + clocks
                        room.next_hand_at = None
                        room.pause_action_timer()
                        if room.tournament:
                            room.pause_tournament_clock()
                    await room.broadcast()

            elif mtype == "action" and room and pid:
                if not room.auto_running and room.game.hand_in_progress:
                    await ws.send_json({"type": "error",
                                        "message": "게임이 일시정지되었습니다. 방장이 재개하면 진행됩니다."})
                else:
                    action = msg.get("action")
                    amount = int(msg.get("amount") or 0)
                    async with room.lock:
                        err = room.game.act(pid, action, amount)
                        if not err:
                            room.arm_timer()    # reset the clock for the next actor
                    if err:
                        await ws.send_json({"type": "error", "message": err})
                    await room.broadcast()
                    await room.persist_new_hands()

            elif mtype == "list_replays" and room:
                if db.enabled():
                    lst = await db.list_hands(room.id)
                else:
                    lst = room.game.replay_list()
                await ws.send_json({"type": "replays", "list": lst})

            elif mtype == "get_replay" and room:
                num = int(msg.get("number") or 0)
                if db.enabled():
                    rec = await db.get_hand(room.id, num)
                else:
                    rec = room.game.get_replay(num)
                await ws.send_json({"type": "replay", "record": rec})

            elif mtype == "chat" and room:
                text = (msg.get("text") or "").strip()[:200]
                if text:
                    async with room.lock:
                        name = room.conn_display_name(ws)   # spectators can chat too
                        room.chat.append({"name": name, "text": text})
                        room.chat = room.chat[-100:]
                    await room.broadcast()

            elif mtype == "sit_out" and room and pid:
                async with room.lock:
                    room.game.set_sitting_out(pid, bool(msg.get("value")))
                await room.broadcast()

            elif mtype == "rebuy" and room and pid:
                async with room.lock:
                    p = room.game._player(pid)
                    amt, err = room.game.rebuy(pid)
                    if not err and p:
                        room.ledger_entry(p.name)["added"] += amt
                if err:
                    await ws.send_json({"type": "error", "message": err})
                await room.broadcast()

            elif mtype == "set_timeout" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 설정을 바꿀 수 있습니다."})
                else:
                    secs = int(msg.get("amount") or DEFAULT_TIMEOUT)
                    async with room.lock:
                        room.timeout_seconds = max(MIN_TIMEOUT, min(MAX_TIMEOUT, secs))
                    await room.broadcast()

            elif mtype == "set_blinds" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 설정을 바꿀 수 있습니다."})
                else:
                    async with room.lock:
                        room.game.set_blinds(msg.get("sb"), msg.get("bb"))
                    await room.broadcast()

            elif mtype == "set_tournament" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 설정을 바꿀 수 있습니다."})
                else:
                    async with room.lock:
                        room.tournament = bool(msg.get("enabled"))
                        mins = int(msg.get("minutes") or room.level_minutes)
                        room.level_minutes = max(MIN_LEVEL_MINUTES,
                                                 min(MAX_LEVEL_MINUTES, mins))
                        levels = msg.get("levels")
                        if isinstance(levels, list) and levels:
                            cleaned = []
                            for lv in levels[:MAX_LEVELS]:
                                try:
                                    sb = max(0, int(lv[0]))
                                    bb = max(1, int(lv[1]))
                                except (TypeError, ValueError, IndexError):
                                    continue
                                cleaned.append([sb, bb])
                            if cleaned:
                                room.blind_levels = cleaned
                        room.level_index = min(room.level_index,
                                               len(room.blind_levels) - 1)
                        if not room.tournament:
                            # Turning it off resets the clock so a later re-enable
                            # starts a fresh tournament at level 1.
                            room.reset_tournament_clock()
                        elif room.tournament and room.auto_running and not room.tourney_active:
                            # Enabled mid-game while already running -> start now.
                            room.start_tournament_clock()
                    await room.broadcast()

            elif mtype == "set_default_stack" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 설정을 바꿀 수 있습니다."})
                else:
                    async with room.lock:
                        room.game.set_default_stack(msg.get("amount"))
                    await room.broadcast()

            elif mtype == "set_variant" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 설정을 바꿀 수 있습니다."})
                else:
                    async with room.lock:
                        room.game.set_variant(msg.get("variant"), msg.get("betting"))
                    await room.broadcast()

            elif mtype == "adjust_stack" and room:
                if pid is None or pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 스택을 조절할 수 있습니다."})
                else:
                    target = msg.get("target")
                    delta = int(msg.get("delta") or 0)
                    async with room.lock:
                        p = room.game._player(target)
                        applied, err = room.game.adjust_stack(target, delta)
                        if not err and p:
                            entry = room.ledger_entry(p.name)
                            if applied >= 0:
                                entry["added"] += applied
                            else:
                                entry["removed"] += -applied
                    if err:
                        await ws.send_json({"type": "error", "message": err})
                    await room.broadcast()

            elif mtype == "leave":
                left_clean = True       # intentional: remove immediately in finally
                break

            elif mtype == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    finally:
        if room and ws in room.connections:
            seated_pid = room.connections.pop(ws)
            room.conn_name.pop(ws, None)
            async with room.lock:
                if seated_pid is not None:           # a seated player dropped
                    if left_clean:
                        room.drop_player(seated_pid)        # intentional leave: remove now
                    else:
                        room.mark_disconnected(seated_pid)  # network drop: hold seat (grace)
                # spectators leave no trace
            await room.broadcast()
            # Stop the clock only when nobody is connected AND nobody is waiting to
            # reconnect (otherwise the grace timer needs to keep ticking).
            if not room.connections and not room.disconnected:
                room.stop_loop()


# Serve the rest of the static files (app.js, style.css) under /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    # Lets you run `python server.py` locally. Hosts like Render inject the port
    # to listen on via the PORT environment variable.
    import os
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
