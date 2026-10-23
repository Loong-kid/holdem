import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from poker.room import Room


async def seat(room, name, now=1000):
    connection = room.connect(name, "ip-" + name)
    await room.handle(connection, {"type": "join", "room": room.id, "name": name, "token": name * 32}, now)
    await room.handle(connection, {"type": "sit", "name": name, "token": name * 32}, now)
    return connection


class RoomTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_room_log_is_bounded(self):
        room = Room("test", 1000)
        room.game.log = [str(i) for i in range(10000)]
        await room.advance(1000)
        self.assertEqual(room.game.log, [str(i) for i in range(9800, 10000)])

    async def test_all_modes_nine_seats_and_restored_command_results(self):
        for variant in ("holdem", "omaha", "omaha2"):
            for betting in ("nl", "pl"):
                with self.subTest(variant=variant, betting=betting):
                    room = Room("test", 1000)
                    connections = [await seat(room, str(i)) for i in range(9)]
                    await room.handle(connections[0], {"type": "set_variant", "variant": variant, "betting": betting}, 1000)
                    await room.handle(connections[0], {"type": "start"}, 1000)
                    for step in range(100):
                        restored = Room.restore(json.loads(json.dumps(room.snapshot())), 1000)
                        self.assertEqual(room.snapshot(), restored.snapshot())
                        room = restored
                        if not room.game.hand_in_progress:
                            break
                        if room.game.awaiting_runout:
                            await room.advance(1000 + step * 2)
                            continue
                        pid = room.game.players[room.game.to_act].id
                        connection = next(w for w, p in room.connections.items() if p == pid)
                        legal = room.game.legal_actions(pid)
                        action = "check" if legal["can_check"] else "call"
                        await room.handle(connection, {"type": "action", "action": action}, 1000)
                    else:
                        self.fail("Hand did not finish")
                    self.assertEqual(sum(p.chips for p in room.game.players), 9000)
                    self.assertEqual(len(room.outbox), 1)
                    self.assertEqual(room.outbox[0]["hand_number"], 1)

    async def test_reconnect_requires_token_and_cancels_duplicate_socket(self):
        room = Room("test", 1000)
        original = await seat(room, "A")
        pid = room.connections[original]
        await room.disconnect(original)
        intruder = room.connect("intruder", "ip-other")
        await room.handle(intruder, {"type": "join", "room": "test", "name": "A", "token": "wrong" * 8}, 1001)
        self.assertIsNone(room.connections[intruder])
        await room.handle(intruder, {"type": "sit", "name": "A", "token": "wrong" * 8}, 1001)
        self.assertEqual(intruder.messages[-1]["type"], "error")
        for connection_id in ("resume", "replace"):
            ws = room.connect(connection_id, "new-ip")
            await room.handle(ws, {"type": "join", "room": "test", "name": "A", "token": "A" * 32}, 1002)
            self.assertEqual(room.connections[ws], pid)
        self.assertEqual(list(room.connections.values()).count(pid), 1)

    async def test_duplicate_action_and_alarm_do_not_apply_twice(self):
        room = Room("test", 1000)
        a, b = await seat(room, "A"), await seat(room, "B")
        await room.handle(a, {"type": "start"}, 1000)
        pid = room.game.players[room.game.to_act].id
        actor = next(w for w, p in room.connections.items() if p == pid)
        command = {"type": "action", "action": "fold", "command_id": "unique-fold"}
        await room.handle(actor, command, 1001)
        room = Room.restore(room.snapshot(), 1002)
        actor = room.connection(actor.id)
        before = room.snapshot()
        await room.handle(actor, command, 1002)
        self.assertEqual(room.snapshot(), before)
        await room.advance(1002)
        await room.advance(1002)
        self.assertEqual(room.game.hand_count, 1)
        self.assertEqual(len(room.outbox), 1)

    async def test_paused_deadline_and_late_tournament_alarm(self):
        room = Room("test", 1000)
        a, b = await seat(room, "A"), await seat(room, "B")
        await room.handle(a, {"type": "set_tournament", "enabled": True,
                                  "minutes": 10, "levels": [[5, 10], [10, 20], [20, 40]]}, 1000)
        await room.handle(a, {"type": "start"}, 1000)
        await room.handle(a, {"type": "pause"}, 1010)
        restored = Room.restore(room.snapshot(), 9000)
        await restored.advance(9000)
        self.assertIsNone(restored.action_deadline)
        self.assertEqual(restored.action_remaining, 20)
        self.assertEqual(restored.level_remaining, 590)
        await restored.handle(restored.connection(a.id), {"type": "start"}, 9000)
        await restored.advance(10210)
        self.assertEqual(restored.level_index, 2)

    async def test_permissions_and_invalid_input_do_not_change_game(self):
        room = Room("test", 1000)
        a, b = await seat(room, "A"), await seat(room, "B")
        before = room.game.public_state()
        await room.handle(b, {"type": "set_blinds", "sb": 50, "bb": 100}, 1000)
        self.assertEqual(room.game.public_state(), before)
        for message in ({"type": "set_blinds", "sb": 50, "bb": None},
                        {"type": "action", "amount": float("nan")},
                        {"type": "set_allow_same_ip", "value": "false"}):
            with self.assertRaises(ValueError):
                await room.handle(a, message, 1000)
            self.assertEqual(room.game.public_state(), before)


if __name__ == "__main__":
    unittest.main()
