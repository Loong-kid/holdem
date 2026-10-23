"""Real workerd + D1 + DO regression, including a full runtime restart.

Run with a Python environment containing websockets (e.g. ../Holdem/.venv).
All mutations use an isolated local persistence directory, never the remote DB.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import shutil
import sys
import time
import urllib.request
import urllib.error
import uuid
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parent.parent


def send(ws, **message):
    ws.send(json.dumps(message))


def until(ws, predicate):
    for _ in range(200):
        value = json.loads(ws.recv(timeout=15))
        if value.get("type") == "error":
            raise AssertionError(value)
        if predicate(value):
            return value
    raise AssertionError("Expected message not received")


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    storage = ROOT / ".wrangler" / ("test-" + uuid.uuid4().hex)
    storage.mkdir(parents=True)
    log = (storage / "runtime.log").open("w", encoding="utf-8")
    process = None

    def request(path):
        try:
            with urllib.request.urlopen(base + path, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as error:
            return json.load(error)

    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable, "tools/cloudflare_manage.py", "dev", "--port", str(port),
                                    "--persist-to", str(storage)], cwd=ROOT, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(150):
            try:
                if request("/health").get("version") == "v38-cloudflare":
                    return
            except Exception:
                pass
            if process.poll() is not None:
                raise RuntimeError("Runtime failed; see " + str(storage / "runtime.log"))
            time.sleep(0.2)
        raise RuntimeError("Runtime did not start")

    def stop():
        nonlocal process
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.terminate()
            process.wait(timeout=15)
        process = None

    def join(name, token, room="restart"):
        ws = connect(base.replace("http", "ws", 1) + "/ws?room=" + room, close_timeout=1, legacy=True)
        send(ws, type="join", room=room, name=name, token=token)
        joined = until(ws, lambda o: o["type"] == "joined")
        return ws, joined

    try:
        start()
        a, _ = join("A", "a" * 32)
        send(a, type="sit", name="A", token="a" * 32)
        aid = until(a, lambda o: o["type"] == "seated")["id"]
        send(a, type="set_allow_same_ip", value=True)
        b, _ = join("B", "b" * 32)
        send(b, type="sit", name="B", token="b" * 32)
        bid = until(b, lambda o: o["type"] == "seated")["id"]
        send(a, type="start")
        state = until(a, lambda o: o["type"] == "state" and o["public"]["hand_in_progress"])
        assert all(p["hole"] is None for p in state["public"]["players"])
        a_hole = state["private"]["hole"]
        send(a, type="pause")
        paused = until(a, lambda o: o["type"] == "state" and not o["public"]["auto_running"]
                       and o["public"]["hand_in_progress"])
        stop()  # Kill the entire real workerd process tree, not just a Python object.
        a.close(); b.close()
        start()
        a, joined = join("A", "a" * 32)
        assert joined["id"] == aid and joined["seated"]
        restored = until(a, lambda o: o["type"] == "state")
        assert restored["private"]["hole"] == a_hole
        assert restored["public"]["pot"] == paused["public"]["pot"]
        assert restored["public"]["time_left"] == paused["public"]["time_left"]
        assert not restored["public"]["auto_running"]
        b, joined = join("B", "b" * 32)
        assert joined["id"] == bid
        send(a, type="start")
        state = until(a, lambda o: o["type"] == "state" and o["public"]["auto_running"])
        actor = a if state["public"]["to_act"] == aid else b
        send(actor, type="action", action="fold", amount=0, command_id="one-fold")
        until(a, lambda o: o["type"] == "state" and not o["public"]["hand_in_progress"])
        send(actor, type="action", action="fold", amount=0, command_id="one-fold")
        until(actor, lambda o: o["type"] == "ack" and o["duplicate"])
        send(a, type="pause")
        until(a, lambda o: o["type"] == "state" and not o["public"]["auto_running"])
        exported = request("/export?room=restart")
        assert exported["count"] == 1
        assert request("/export?room=other")["count"] == 0
        assert request("/gto?room=restart")["count"] == 1
        send(a, type="list_replays")
        listing = until(a, lambda o: o["type"] == "replays")["list"]
        assert len(listing) == 1
        send(a, type="get_replay", number=listing[0]["number"])
        replay = until(a, lambda o: o["type"] == "replay")["record"]
        assert replay["events"] == exported["hands"][0]["events"]
        # Make only this test's isolated local D1 unavailable, retaining its rows.
        def local_sql(sql):
            subprocess.run([shutil.which("npm.cmd") or shutil.which("npm"), "exec", "wrangler", "--",
                            "d1", "execute", "DB", "--command", sql, "--local", "--persist-to", str(storage)],
                           cwd=ROOT, stdout=log, stderr=log, check=True)
        local_sql("ALTER TABLE hands RENAME TO hands_offline_fixture")
        send(a, type="start")
        state = until(a, lambda o: o["type"] == "state" and o["public"]["hand_in_progress"])
        actor = a if state["public"]["to_act"] == aid else b
        send(actor, type="action", action="fold")
        until(a, lambda o: o["type"] == "state" and not o["public"]["hand_in_progress"])
        send(a, type="pause")
        until(a, lambda o: o["type"] == "state" and not o["public"]["auto_running"])
        assert not request("/health")["db"]
        stop(); a.close(); b.close(); start()
        a, joined = join("A", "a" * 32)
        assert joined["id"] == aid
        local_sql("ALTER TABLE hands_offline_fixture RENAME TO hands")
        b, joined = join("B", "b" * 32)
        assert joined["id"] == bid
        send(a, type="ping")
        until(a, lambda o: o["type"] == "pong")
        assert request("/export?room=restart")["count"] == 2
        send(a, type="leave"); send(b, type="leave")
        a.close(); b.close()
        stop(); start()
        assert request("/export?room=restart")["count"] == 2
        print("PASS: real runtime restart, same seat/cards/pot/paused clocks, duplicate command, D1 replay, isolation, GTO, D1 outage + restart + outbox recovery", flush=True)
    finally:
        stop()
        log.close()


if __name__ == "__main__":
    main()
