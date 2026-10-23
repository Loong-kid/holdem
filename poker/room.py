"""Host-independent room commands and clocks. No network or database I/O.

Connections queue messages until the runtime commits the room snapshot. The
runtime supplies Unix seconds; process-local monotonic timestamps are never saved.
"""
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from .game import Game, summarize_hand
from .snapshots import snapshot_game, restore_game, SnapshotError

@dataclass(eq=False)
class Connection:
    id: str
    ip: str = ""
    messages: list = field(default_factory=list)
    closed: bool = False

    async def send_json(self, message):
        self.messages.append(message)

    async def send_text(self, message):
        self.messages.append(json.loads(message))

    async def close(self):
        self.closed = True

NEXT_HAND_DELAY = 5.0      # seconds to show results before auto-dealing the next hand
RUNOUT_DELAY = 1.3         # seconds between board reveals on an all-in run-out
DEFAULT_TIMEOUT = 30       # seconds per action
MIN_TIMEOUT, MAX_TIMEOUT = 20, 60
DISCONNECT_GRACE = 120     # seconds a dropped player keeps cards; then -> sit-out (seat kept)
APP_VERSION = "v38-cloudflare"   # bump on deploy so we can confirm what's live

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


class Room:
    def __init__(self, room_id: str, now: float = 0):
        self.now = now
        self.session_id = uuid.uuid4().hex
        self.revision = 0
        self.outbox = []
        self.seen_commands = []
        self.id = room_id
        self.game = Game(small_blind=5, big_blind=10, starting_chips=1000)
        # websocket -> seated player id, or None for a spectator (watching only).
        self.connections: dict[Connection, str | None] = {}
        self.conn_name: dict[Connection, str] = {}   # ws -> display name (sit / chat)
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

    def _now(self) -> float:
        return self.now

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

        # Grace expired: instead of removing the player (which loses the seat and
        # forces a fresh login on mobile, where the reconnect timer is frozen in the
        # background), keep the seat + token and just sit them out. They auto-resume
        # via their browser token whenever they come back.
        if self.disconnected:
            for pid in [k for k, dl in self.disconnected.items() if now >= dl]:
                self.disconnected.pop(pid, None)
                p = self.game._player(pid)
                if p:
                    self.game.set_sitting_out(pid, True)
                    self.game.log.append(f"{p.name} 자리비움 (연결 끊김 — 좌석 유지, 재접속 시 복귀).")
                changed = True

        # Tournament blind clock: independent of whose turn it is. Advancing a
        # level just changes the blinds; they apply on the next hand (engine
        # rule), so it is safe to fire even mid-hand.
        if self.tournament and self.auto_running and self.level_deadline is not None:
            if now >= self.level_deadline:
                if self.level_index < len(self.blind_levels) - 1:
                    self.level_index += 1
                    self.apply_level_blinds()
                    self.level_deadline += self.level_minutes * 60
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
        public["db"] = True
        public["version"] = APP_VERSION
        public["tournament"] = self.tournament_state()
        public["allow_same_ip"] = self.allow_same_ip
        public["spectators"] = sum(1 for v in self.connections.values() if v is None)
        public["spectator_names"] = [self.conn_name.get(ws, "관전자")
                                     for ws, v in self.connections.items() if v is None]
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
        # public은 모든 수신자에게 동일하므로 한 번만 직렬화해서 재사용한다.
        # (기존엔 연결 수만큼 public을 매번 직렬화 -> 7명이면 7배 CPU 낭비.)
        public_json = json.dumps(public, ensure_ascii=False)
        # Iterate a SNAPSHOT: send awaits, and a concurrent join/leave/reconnect can
        # mutate self.connections during that await. Iterating the live dict would
        # raise "dictionary changed size during iteration" and kill the broadcast.
        for ws, pid in list(self.connections.items()):
            priv_json = json.dumps(self.game.private_state(pid), ensure_ascii=False)
            msg = '{"type":"state","public":' + public_json + ',"private":' + priv_json + '}'
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            pid = self.connections.pop(ws, None)
            # A failed send means the socket is gone: start their grace period so
            # they can reconnect instead of being silently stuck.
            if pid and pid not in self.disconnected and self.game._player(pid):
                self.mark_disconnected(pid)

    async def _handle(self, ws, msg):
        room = self
        pid = self.connections.get(ws)
        mtype = msg.get("type")
        if mtype == "join":
            # Joining a room = watching as a SPECTATOR. You only get a seat
            # (cards/chips) after pressing "테이블에 앉기" (the "sit" message).
            # Exception: if this browser already owns a seat (token) or is a
            # dropped player within the grace window, we resume that seat.
            room_id = (msg.get("room") or "main").strip() or "main"
            name = (msg.get("name") or "Player").strip()[:16] or "Player"
            token = (msg.get("token") or "").strip() or None
            ip = ws.ip
            assert room_id == self.id, "Room routing mismatch"
            stale_ws = []
            async with room.lock:
                room.conn_name[ws] = name
                bound = room.pid_for_token(token)
                if bound is not None:
                    pid = bound                     # resume the existing seat
                    # 같은 좌석을 점유하던 옛(좀비) 연결을 끊는다 -> 한 사람 = 한 연결.
                    # 모바일 재접속 등으로 죽은 소켓이 남아 broadcast 대상이 부풀고
                    # outbound/CPU가 시간에 따라 누적되던 문제를 막는다.
                    stale_ws = [w for w, p in list(room.connections.items())
                                if p == pid and w is not ws]
                    for w in stale_ws:
                        room.connections.pop(w, None)
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
            for w in stale_ws:                       # close outside the lock (best-effort)
                try:
                    await w.close()
                except Exception:
                    pass
            await ws.send_json({"type": "joined", "id": pid, "room": room_id,
                                "name": seated_name, "seated": pid is not None})
            pass  # Cloudflare schedules the next absolute deadline.
            await room.broadcast()

        elif mtype == "sit" and room and pid is None:
            # Spectator takes a seat. IP / nickname / capacity checks happen here.
            name = (msg.get("name") or room.conn_name.get(ws) or "Player").strip()[:16] or "Player"
            token = (msg.get("token") or "").strip() or None
            ip = ws.ip
            candidate = uuid.uuid4().hex[:8]
            error = None
            async with room.lock:
                room.conn_name[ws] = name
                entry = room.ledger.get(name)
                # 같은 닉의 좌석이 이미 테이블에 있으면 절대 새 좌석을 만들지 않는다.
                # (ledger active 플래그만 믿으면 끊김 grace/자리비움으로 남은 좌석과
                #  중복 착석이 가능 -> start 스냅샷에 같은 닉 2좌석 = 통계 이중 계산 버그)
                # 살아있는 연결이 없는 좌석(끊김 잔류)이면 그 좌석을 그대로 이어받는다.
                existing = next((p for p in room.game.players
                                 if p.name == name and not p.pending_removal), None)
                if existing is not None:
                    if room.pid_for_token(token) != existing.id:
                        error = "이미 사용 중인 닉네임입니다. 다른 이름을 써주세요."
                    else:
                        pid = existing.id          # 한 이름 = 한 좌석 (승계)
                        room.connections[ws] = pid
                        room.disconnected.pop(pid, None)
                        if token:
                            room.tokens[token] = pid
                        room.player_ip[pid] = ip
                        if entry is not None:
                            entry["active"] = True
                elif (not room.allow_same_ip) and room.pid_for_ip(ip) is not None:
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

        elif mtype in ("list_replays", "get_replay"):
            pass  # Repository queries are handled by the runtime adapter.

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
            await self.disconnect(ws, clean=True)
            return

        elif mtype == "ping":
            await ws.send_json({"type": "pong"})

    def connect(self, connection_id, ip=""):
        connection = Connection(connection_id, ip)
        self.connections[connection] = None
        self.conn_name[connection] = ""
        return connection

    def connection(self, connection_id):
        return next((ws for ws in self.connections if ws.id == connection_id), None)

    def next_deadline(self):
        deadlines = [self.action_deadline, self.next_hand_at, self.runout_at,
                     self.level_deadline, *self.disconnected.values()]
        return min((d for d in deadlines if d is not None), default=None)

    async def persist_new_hands(self):
        for rec in self.game.hand_log:
            if rec["number"] > self.persisted_count:
                self.outbox.append({
                    "session_id": self.session_id, "room": self.id,
                    "hand_number": rec["number"], "title": summarize_hand(rec),
                    "played_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                    "events": rec["events"],
                })
                self.persisted_count = rec["number"]

    async def advance(self, now):
        self.now = now
        changed = False
        # Catch up overdue blind levels, but never repeatedly time out a new actor.
        for _ in range(MAX_LEVELS + 2):
            if not self._tick():
                break
            changed = True
        if not self.connections and not self.disconnected:
            self.auto_running = False
            self.pause_action_timer()
            self.pause_tournament_clock()
            self.next_hand_at = self.runout_at = None
        # The UI displays only the latest 30 entries. Persistent rooms must not
        # accumulate an unbounded text log across months of play.
        self.game.log = self.game.log[-200:]
        await self.persist_new_hands()
        if changed:
            self.revision += 1
            await self.broadcast()

    async def disconnect(self, ws, clean=False):
        if ws not in self.connections:
            return
        pid = self.connections.pop(ws)
        self.conn_name.pop(ws, None)
        if pid is not None:
            if clean:
                self.drop_player(pid)
            else:
                self.mark_disconnected(pid)
        ws.closed = True
        await self.advance(self.now)
        await self.broadcast()

    async def handle(self, ws, message, now):
        self.now = now
        if type(message) is not dict or type(message.get("type")) is not str:
            raise ValueError("Invalid message")
        if ws not in self.connections:
            raise ValueError("Connection is no longer active")
        if message["type"] == "join" and self.conn_name.get(ws):
            raise ValueError("Connection already joined")
        if message["type"] != "join" and not self.conn_name.get(ws):
            raise ValueError("Join a room first")
        if message["type"] == "set_blinds" and any(type(message.get(k)) is not int for k in ("sb", "bb")):
            raise ValueError("Both blinds must be integers")
        for key in ("value", "enabled"):
            if key in message and type(message[key]) is not bool:
                raise ValueError("Invalid boolean: " + key)
        for key in ("amount", "delta", "number", "minutes", "sb", "bb"):
            value = message.get(key)
            if value is not None and (type(value) is not int or abs(value) > 10**12):
                raise ValueError("Invalid integer: " + key)
        for key in ("name", "room", "token", "text", "action", "target", "variant", "betting", "command_id"):
            value = message.get(key)
            if value is not None and (type(value) is not str or len(value) > 2048):
                raise ValueError("Invalid text: " + key)
        if message["type"] == "join" and message.get("room", self.id) != self.id:
            raise ValueError("Room routing mismatch")
        if message["type"] == "sit" and len(message.get("token") or "") < 16:
            raise ValueError("A browser token is required to own a seat")
        command_id = message.get("command_id")
        owner = self.connections[ws] or ws.id
        key = str(owner) + ":" + command_id if command_id else None
        if key and key in self.seen_commands:
            await ws.send_json({"type": "ack", "command_id": command_id, "duplicate": True})
            return
        await self.advance(now)
        await self._handle(ws, message)
        if key:
            self.seen_commands = (self.seen_commands + [key])[-512:]
        self.revision += 1
        await self.advance(now)

    def snapshot(self):
        fields = (
            "tokens", "player_ip", "allow_same_ip", "host_id", "ledger", "chat",
            "disconnected", "auto_running", "timeout_seconds", "action_deadline",
            "action_remaining", "next_hand_at", "runout_at", "persisted_count",
            "tournament", "level_minutes", "blind_levels", "level_index",
            "level_deadline", "level_remaining", "tourney_active", "outbox", "seen_commands",
        )
        return json.loads(json.dumps({
            "version": 1, "room_id": self.id, "session_id": self.session_id,
            "revision": self.revision, "game": snapshot_game(self.game),
            "state": {key: getattr(self, key) for key in fields},
            "connections": [{"id": ws.id, "ip": ws.ip, "pid": pid,
                             "name": self.conn_name.get(ws, "")}
                            for ws, pid in self.connections.items()],
        }, allow_nan=False))

    @classmethod
    def restore(cls, data, now):
        if type(data) is not dict or data.get("version") != 1:
            raise SnapshotError("Unsupported room snapshot")
        room = cls(data["room_id"], now)
        room.game = restore_game(data["game"])
        expected = set(room.snapshot()["state"])
        if set(data["state"]) != expected:
            raise SnapshotError("Incomplete room snapshot")
        for key, value in json.loads(json.dumps(data["state"], allow_nan=False)).items():
            setattr(room, key, value)
        room.session_id = data["session_id"]
        room.revision = data["revision"]
        for row in data["connections"]:
            ws = room.connect(row["id"], row["ip"])
            room.connections[ws] = row["pid"]
            room.conn_name[ws] = row["name"]
        return room
