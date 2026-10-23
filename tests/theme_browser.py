"""Render actual engine states through the production UI; never connect or write to a room.

Usage: uv run --group test python tests/theme_browser.py [URL]
The separately run cloudflare_browser.py covers live commands, sockets and D1.
"""
import asyncio
import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from poker.room import Room


async def fixture(count, variant, phase):
    room = Room("금요일 홀덤", 1000)
    connections = []
    names = ["민준", "도윤", "서연", "지우", "현우", "수빈", "지호", "예린", "하준"]
    for i in range(count):
        connection = room.connect(str(i), "ip-" + str(i))
        await room.handle(connection, {"type": "join", "room": room.id, "name": names[i], "token": str(i) * 32}, 1000)
        await room.handle(connection, {"type": "sit", "name": names[i], "token": str(i) * 32}, 1000)
        connections.append(connection)
    await room.handle(connections[0], {"type": "set_variant", "variant": variant, "betting": "nl"}, 1000)
    await room.handle(connections[0], {"type": "start"}, 1000)
    for _ in range(40):
        if room.game.phase == phase:
            break
        pid = room.game.players[room.game.to_act].id
        connection = next(w for w, p in room.connections.items() if p == pid)
        legal = room.game.legal_actions(pid)
        await room.handle(connection, {"type": "action", "action": "check" if legal["can_check"] else "call"}, 1000)
    assert room.game.phase == phase
    actor = room.game.players[room.game.to_act].id
    connection = next(w for w, p in room.connections.items() if p == actor)
    await room.broadcast()
    message = next(m for m in reversed(connection.messages) if m["type"] == "state")
    # Make the viewer the host to exercise the full header/settings at every size.
    message["public"]["host"] = actor
    return {"id": actor, "public": message["public"], "private": message["private"]}


base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
out = ROOT / "tests/shots"
out.mkdir(exist_ok=True)
cases = {(n, v, phase): asyncio.run(fixture(n, v, phase))
         for n in (2, 6, 9) for v in ("holdem", "omaha", "omaha2") for phase in ("flop", "river")}
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    sockets = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("websocket", lambda socket: sockets.append(socket.url))
    page.goto(base)
    assert page.locator('link[href*="theme.css"]').count() == 1
    for width in (320, 375, 390, 414, 768, 1024, 1400, 1920):
        page.set_viewport_size({"width": width, "height": 844 if width <= 600 else 1000})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=str(out / "theme_join_desktop.png"), full_page=True)
    report = []
    for width in (320, 375, 390, 414, 768, 1024, 1400, 1920):
        page.set_viewport_size({"width": width, "height": 844 if width <= 600 else 1000})
        for (count, variant, phase), value in cases.items():
            page.evaluate("""f => {
              soundOn=false; myId=f.id; state=f.public; priv=f.private;
              enterGame('금요일 홀덤','나'); render();
            }""", value)
            assert page.locator(".seat").count() == count
            assert page.locator("#action-bar").is_visible()
            assert page.locator("#community .card").count() == 5 * (2 if variant == "omaha2" else 1)
            assert page.locator("#community .card:not(.placeholder)").count() == (5 if phase == "river" else 3) * (2 if variant == "omaha2" else 1)
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"), (width, count, variant, phase)
            collisions = page.evaluate("""() => {
              const community=document.querySelector('#community').getBoundingClientRect();
              const intersect=(a,b)=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>2 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>2;
              const plates=[...document.querySelectorAll('.seat .nameplate')].map(el=>el.getBoundingClientRect());
              return {board:plates.filter(r=>intersect(r,community)).length,
                seats:plates.reduce((n,a,i)=>n+plates.slice(i+1).filter(b=>intersect(a,b)).length,0)};
            }""")
            report.append({"width": width, "players": count, "variant": variant, "phase": phase, **collisions})
            if (width, count, variant, phase) in ((1400, 6, "holdem", "flop"), (390, 6, "holdem", "flop"),
                                                (1400, 9, "omaha2", "river"), (390, 9, "omaha2", "river")):
                page.screenshot(path=str(out / f"theme_{width}_{count}_{variant}_{phase}.png"), full_page=True)
    page.locator("#settings-btn").click()
    assert "D1" in page.locator("#db-status").inner_text()
    page.locator('[data-close="settings-modal"]').click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#chat-toggle").click()
    assert page.locator("#chat").is_visible()
    assert page.locator("#chat-toggle").get_attribute("aria-expanded") == "true"
    page.locator('#chat .panel-handle').click()
    assert not page.locator("#chat").is_visible()
    assert page.locator("#chat-toggle").get_attribute("aria-expanded") == "false"
    assert not errors, errors
    assert not sockets, sockets
    browser.close()
(out / "theme_validation.json").write_text(json.dumps({"url": base, "cases": report, "errors": errors}, indent=2), encoding="utf-8")
bad = [row for row in report if row["board"] or row["seats"]]
assert not bad, bad
print(f"PASS {len(report)} game layouts + entry, settings, drawers; no board/seat collisions, horizontal overflow or JS errors")
