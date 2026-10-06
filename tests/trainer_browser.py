"""Test the integrated site in desktop/mobile Chrome. Starts an isolated server."""
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests/shots"
OUT.mkdir(parents=True, exist_ok=True)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
url = f"http://127.0.0.1:{port}"
python = os.environ.get("SERVER_PYTHON", str(ROOT / ".venv/Scripts/python.exe"))
server = subprocess.Popen([python, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port)],
                          cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
try:
    for attempt in range(100):
        try:
            urllib.request.urlopen(url + "/health", timeout=1).close()
            break
        except OSError:
            if server.poll() is not None:
                raise RuntimeError("Test server failed to start")
            time.sleep(0.1)
    else:
        raise RuntimeError("Test server did not become ready")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get("PLAYWRIGHT_CHANNEL", "chrome"))
        for name, viewport, mobile in [("desk", {"width": 1400, "height": 900}, False), ("phone", {"width": 390, "height": 844}, True)]:
            ctx = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile)
            errors = []
            ctx.on("page", lambda page: page.on("pageerror", lambda e: errors.append(str(e))))
            page = ctx.new_page()
            page.goto(url)
            assert page.locator("#join-trainer-link").is_visible()
            page.screenshot(path=str(OUT / f"{name}_join.png"))
            with page.expect_popup() as opening:
                page.locator("#join-trainer-link").click()
            trainer = opening.value
            trainer.wait_for_load_state()
            assert trainer.url == url + "/trainer/"
            assert trainer.locator(".stage").count() == 8
            assert trainer.locator("#home .site-link").is_visible()
            trainer.evaluate("prog.settings.sound=false; prog.stages.open={n:60,ok:50}; prog.stages.vsopen={n:60,ok:45}; renderHome();")
            for stage in ["open", "push", "vsopen", "rejam", "hu", "bvb", "bb", "special"]:
                trainer.evaluate("id => startStage(id,'all')", stage)
                assert trainer.locator("#actions .act").count() >= 2
                trainer.evaluate("answer(Ranges.bestActions(q.rule)[0])")
                assert trainer.locator("#feedback").is_visible()
                assert trainer.locator("#mini > div").count() == 169
                assert "undefined" not in trainer.inner_text("main")
                assert "NaN" not in trainer.inner_text("main")
                assert not trainer.evaluate("document.documentElement.scrollWidth > innerWidth")
                trainer.locator("#home-btn").click()
            trainer.locator("#library-open").click()
            assert trainer.locator("#library-spot option").count() == 177
            trainer.keyboard.press("Escape")
            trainer.screenshot(path=str(OUT / f"{name}_trainer_home.png"))
            saved = trainer.evaluate("JSON.stringify(prog)")
            trainer.reload()
            assert trainer.evaluate("JSON.stringify(prog)") == saved
            trainer.close()
            # Enter the live room; opening training keeps the original WebSocket.
            page.locator("#name-input").fill("TrainerTest_" + name)
            page.locator("#room-input").fill("trainer-ui-" + name)
            page.locator("#join-btn").click()
            page.wait_for_selector("#game-screen:not(.hidden)")
            page.wait_for_function("ws && ws.readyState === WebSocket.OPEN && state !== null")
            snapshot = page.evaluate("({room:myRoom, id:myId, n:state.hand_number})")
            assert page.locator("#trainer-link").is_visible()
            page.screenshot(path=str(OUT / f"{name}_game.png"))
            with page.expect_popup() as opening:
                page.locator("#trainer-link").click()
            training = opening.value
            training.wait_for_selector("#stage-list .stage")
            assert page.evaluate("ws.readyState") == 1
            assert page.evaluate("({room:myRoom, id:myId, n:state.hand_number})") == snapshot
            training.locator("#home .site-link").click()
            training.wait_for_selector("#join-btn")
            assert training.url == url + "/"
            assert not errors, errors
            print(name, "입장/게임 버튼·8단계·177차트·기록 보존·홀덤 연결 유지 통과")
            ctx.close()
        browser.close()
finally:
    server.terminate()
    server.wait(timeout=10)
