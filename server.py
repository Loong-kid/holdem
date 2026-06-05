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
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from poker.game import Game

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI()


class Room:
    """One poker table: a Game plus the set of connected browsers."""

    def __init__(self, room_id: str):
        self.id = room_id
        self.game = Game(small_blind=5, big_blind=10, starting_chips=1000)
        # websocket -> player id
        self.connections: dict[WebSocket, str] = {}
        self.host_id: str | None = None   # first player to join owns the "Deal" button
        self.lock = asyncio.Lock()   # serialize actions so the engine sees one at a time
        # Cash-game ledger keyed by nickname. Survives a player leaving so the
        # leaderboard keeps their record. Tracks money in/out of the table.
        #   buyin   = chips bought in with on first sit
        #   added   = chips topped up later (host action)
        #   removed = chips taken off the table (host action)
        #   last_stack = most recent chip count (live, or frozen when they left)
        #   active  = is someone currently connected under this name
        self.ledger: dict[str, dict] = {}

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
        public["ledger"] = self.ledger_view()
        dead = []
        for ws, pid in self.connections.items():
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
            self.connections.pop(ws, None)


class RoomManager:
    def __init__(self):
        self.rooms: dict[str, Room] = {}

    def get(self, room_id: str) -> Room:
        if room_id not in self.rooms:
            self.rooms[room_id] = Room(room_id)
        return self.rooms[room_id]


manager = RoomManager()


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    room: Room | None = None
    pid: str | None = None
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "join":
                room_id = (msg.get("room") or "main").strip() or "main"
                name = (msg.get("name") or "Player").strip()[:16] or "Player"
                room = manager.get(room_id)
                candidate = uuid.uuid4().hex[:8]
                error = None
                async with room.lock:
                    entry = room.ledger.get(name)
                    if entry and entry["active"]:
                        error = "이미 사용 중인 닉네임입니다. 다른 이름을 써주세요."
                    elif room.game.is_full():
                        error = "테이블이 가득 찼습니다 (최대 9명)."
                    elif entry is not None:
                        # Same name returning -> restore their previous stack.
                        player = room.game.add_player(candidate, name, entry["last_stack"])
                        entry["active"] = True
                    else:
                        # Brand new player -> default buy-in.
                        start = room.game.starting_chips
                        player = room.game.add_player(candidate, name, start)
                        room.ledger[name] = {"buyin": start, "added": 0, "removed": 0,
                                             "last_stack": start, "active": True}
                    if error is None:
                        pid = candidate
                        room.connections[ws] = pid
                        if room.host_id is None:        # first arrival becomes host
                            room.host_id = pid
                if error is not None:
                    await ws.send_json({"type": "error", "message": error})
                else:
                    await ws.send_json({"type": "joined", "id": pid, "room": room_id})
                    await room.broadcast()

            elif mtype == "start" and room:
                if pid != room.host_id:
                    await ws.send_json({"type": "error",
                                        "message": "방장만 딜을 시작할 수 있습니다."})
                else:
                    async with room.lock:
                        room.game.start_hand()
                    await room.broadcast()

            elif mtype == "action" and room and pid:
                action = msg.get("action")
                amount = int(msg.get("amount") or 0)
                async with room.lock:
                    err = room.game.act(pid, action, amount)
                if err:
                    await ws.send_json({"type": "error", "message": err})
                await room.broadcast()

            elif mtype == "set_blinds" and room:
                if pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 설정을 바꿀 수 있습니다."})
                else:
                    async with room.lock:
                        room.game.set_blinds(msg.get("sb"), msg.get("bb"))
                    await room.broadcast()

            elif mtype == "set_default_stack" and room:
                if pid != room.host_id:
                    await ws.send_json({"type": "error", "message": "방장만 설정을 바꿀 수 있습니다."})
                else:
                    async with room.lock:
                        room.game.set_default_stack(msg.get("amount"))
                    await room.broadcast()

            elif mtype == "adjust_stack" and room:
                if pid != room.host_id:
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

            elif mtype == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    finally:
        if room and ws in room.connections:
            leaving = room.connections.pop(ws)
            async with room.lock:
                gone = room.game._player(leaving)
                if gone and gone.name in room.ledger:
                    # Freeze their ledger row; they can reclaim the stack by
                    # rejoining under the same nickname.
                    room.ledger[gone.name]["last_stack"] = gone.chips
                    room.ledger[gone.name]["active"] = False
                room.game.remove_player(leaving)
                if room.host_id == leaving:   # host left -> pass the crown to anyone left
                    room.host_id = room.game.players[0].id if room.game.players else None
            await room.broadcast()


# Serve the rest of the static files (app.js, style.css) under /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    # Lets you run `python server.py` locally. Hosts like Render inject the port
    # to listen on via the PORT environment variable.
    import os
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
