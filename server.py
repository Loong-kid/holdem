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
        self.lock = asyncio.Lock()   # serialize actions so the engine sees one at a time

    async def broadcast(self):
        """Send the latest state to every connected player (public + their private)."""
        public = self.game.public_state()
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
                pid = uuid.uuid4().hex[:8]
                room.connections[ws] = pid
                async with room.lock:
                    room.game.add_player(pid, name)
                await ws.send_json({"type": "joined", "id": pid, "room": room_id})
                await room.broadcast()

            elif mtype == "start" and room:
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

            elif mtype == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    finally:
        if room and ws in room.connections:
            leaving = room.connections.pop(ws)
            async with room.lock:
                room.game.remove_player(leaving)
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
