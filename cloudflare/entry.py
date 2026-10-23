"""Cloudflare-native server: Workers + hibernating room DOs + D1."""
import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

from workers import WorkerEntrypoint, DurableObject, Response
from js import WebSocketPair
from pyodide.ffi import create_proxy

from poker.room import Room, APP_VERSION
from repository import HandRepository
from chart_data import CHART_DB
from gto.charts import ChartProvider
from gto.score import build_report


def json_response(value, status=200):
    return Response(json.dumps(value, ensure_ascii=False, allow_nan=False), status=status,
                    headers={"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"})


def room_name(url):
    name = parse_qs(urlparse(url).query).get("room", ["main"])[0].strip() or "main"
    if len(name.encode()) > 160 or any(ord(c) < 32 for c in name):
        raise ValueError("Invalid room name")
    return name


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        parsed = urlparse(request.url)
        path = parsed.path
        if request.method != "GET":
            return json_response({"error": "Method not allowed"}, 405)
        try:
            if path == "/ws":
                if request.headers.get("Upgrade", "").lower() != "websocket":
                    return Response("WebSocket upgrade required", status=426)
                origin = request.headers.get("Origin")
                if origin and urlparse(origin).netloc != parsed.netloc:
                    return Response("Origin mismatch", status=403)
                name = room_name(request.url)
                return await self.env.ROOMS.getByName("room:" + name).fetch(request)
            repository = HandRepository(self.env.DB)
            if path in ("/health", "/keepalive"):
                try:
                    count = await repository.count()
                    return json_response({"ok": True, "db": True, "persistence": "cloudflare-d1",
                                          "hands": count, "version": APP_VERSION})
                except Exception:
                    return json_response({"ok": False, "db": False, "persistence": "cloudflare-d1",
                                          "error": "D1 is not ready; apply migrations", "version": APP_VERSION}, 503)
            if path in ("/export", "/gto", "/gto_charts"):
                name = room_name(request.url)
                return await self.env.ROOMS.getByName("analysis:" + name).fetch(request)
            return await self.env.ASSETS.fetch(request)
        except ValueError as error:
            return json_response({"error": str(error)}, 400)


class PokerRoom(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.room = None
        self.gate = asyncio.Lock()
        self.repository = HandRepository(env.DB)

    async def _load(self, name=None):
        if self.room is None:
            raw = await self.ctx.storage.get("room")
            if raw:
                self.room = Room.restore(json.loads(str(raw)), time.time())
            elif name is not None:
                self.room = Room(name, time.time())
            else:
                raise ValueError("Room snapshot is missing")
            active = {str(ws.deserializeAttachment()) for ws in self.ctx.getWebSockets()}
            disconnected = False
            for connection in list(self.room.connections):
                if connection.id not in active:
                    await self.room.disconnect(connection)
                    disconnected = True
            if disconnected:
                await self._save()
        if name is not None and self.room.id != name:
            raise ValueError("Room routing mismatch")

    async def _save(self):
        due = self.room.next_deadline()
        if self.room.outbox:
            retry = time.time() + 30
            due = min(due, retry) if due is not None else retry
        payload = json.dumps(self.room.snapshot(), ensure_ascii=False, allow_nan=False)

        async def transaction(tx):
            await tx.put("room", payload)
            if due is None:
                await tx.deleteAlarm()
            else:
                await tx.setAlarm(max(math.ceil(due * 1000), math.ceil(time.time() * 1000) + 1))

        callback = create_proxy(transaction)
        try:
            await self.ctx.storage.transaction(callback)
        finally:
            callback.destroy()

    async def _flush(self):
        changed = False
        for hand in list(self.room.outbox):
            try:
                await self.repository.save(hand)
            except Exception as error:
                print("D1 replay deferred; durable outbox retained:", type(error).__name__)
                break
            self.room.outbox.remove(hand)
            changed = True
        if changed:
            await self._save()

    async def _deliver(self):
        for socket in self.ctx.getWebSockets():
            cid = str(socket.deserializeAttachment())
            connection = self.room.connection(cid)
            if connection is None or connection.closed:
                socket.close(1000, "Session replaced or left")
                continue
            messages, connection.messages = connection.messages, []
            try:
                for message in messages:
                    socket.send(json.dumps(message, ensure_ascii=False))
            except Exception:
                await self.room.disconnect(connection)
                await self._save()

    async def fetch(self, request):
        async with self.gate:
            name = room_name(request.url)
            path = urlparse(request.url).path
            if path == "/gto_charts":
                return json_response({"ok": True, "charts": ChartProvider(CHART_DB).library()})
            if path in ("/export", "/gto"):
                hands = await self.repository.export_hands(name)
                payload = {"room": name, "count": len(hands), "hands": hands,
                           "exported_at": datetime.now(timezone.utc).isoformat()}
                if path == "/gto":
                    return json_response({"ok": True, "room": name, "count": len(hands),
                                          **build_report(payload, CHART_DB)})
                return json_response(payload)
            await self._load(name)
            client, server = WebSocketPair.new().object_values()
            cid = uuid.uuid4().hex
            self.ctx.acceptWebSocket(server)
            server.serializeAttachment(cid)
            self.room.connect(cid, request.headers.get("CF-Connecting-IP", "local"))
            await self._save()
            return Response(None, status=101, web_socket=client)

    async def webSocketMessage(self, socket, message):
        async with self.gate:
            try:
                await self._load()
                if not isinstance(message, str) or len(message.encode()) > 16384:
                    raise ValueError("JSON message exceeds 16KB or is not text")
                payload = json.loads(message)
                connection = self.room.connection(str(socket.deserializeAttachment()))
                if connection is None:
                    socket.close(1008, "Session expired")
                    return
                if payload.get("type") in ("list_replays", "get_replay"):
                    if payload["type"] == "list_replays":
                        value = {"type": "replays", "list": await self.repository.list_hands(self.room.id)}
                    else:
                        number = payload.get("number")
                        if type(number) is not int:
                            raise ValueError("Invalid replay number")
                        value = {"type": "replay", "record": await self.repository.get_hand(self.room.id, number)}
                    await connection.send_json(value)
                else:
                    await self.room.handle(connection, payload, time.time())
                # Commit the authoritative result before any client receives it.
                await self._save()
                await self._flush()
                await self._deliver()
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                socket.send(json.dumps({"type": "error", "message": str(error)}))
            except Exception:
                self.room = None  # Reload committed state after a failed storage operation.
                socket.close(1011, "Storage operation failed; reconnect")
                raise

    async def alarm(self, alarm_info=None):
        async with self.gate:
            await self._load()
            await self.room.advance(time.time())
            await self._save()
            await self._flush()
            await self._deliver()

    async def webSocketClose(self, socket, code, reason, was_clean):
        async with self.gate:
            await self._load()
            connection = self.room.connection(str(socket.deserializeAttachment()))
            if connection is not None:
                self.room.now = time.time()
                await self.room.disconnect(connection)
                await self._save()
                await self._deliver()

    async def webSocketError(self, socket, error):
        await self.webSocketClose(socket, 1011, "Socket error", False)
