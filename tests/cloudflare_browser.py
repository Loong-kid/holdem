"""Browser smoke on an explicit URL; uses one uniquely named disposable room."""
import json
from pathlib import Path
import sys
import uuid
from playwright.sync_api import sync_playwright

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
room = "__cf_verify_" + uuid.uuid4().hex[:12]  # Existing room input has maxlength=24.
out = Path(__file__).resolve().parent / "shots"
out.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    contexts, pages, errors = [], [], []
    try:
        for name, viewport, mobile in [("desktop", {"width": 1400, "height": 900}, False),
                                      ("phone", {"width": 390, "height": 844}, True)]:
            ctx = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile)
            contexts.append(ctx)
            ctx.on("page", lambda pg: pg.on("pageerror", lambda error: errors.append(str(error))))
            page = ctx.new_page()
            pages.append(page)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.locator("#name-input").fill("Verify_" + name)
            page.locator("#room-input").fill(room)
            page.locator("#join-btn").click()
            page.wait_for_function("state!==null && ws.readyState===1")
            page.locator("#take-seat-btn").click()
            page.wait_for_function("myId!==null")
            if name == "desktop":
                page.evaluate("sendMessage({type:'set_allow_same_ip',value:true})")
                page.wait_for_function("state.allow_same_ip")
            assert not page.evaluate("document.documentElement.scrollWidth>innerWidth")
        host, guest = pages
        host.wait_for_function("state.players.length===2")
        ids = [page.evaluate("myId") for page in pages]
        for variant in ("holdem", "omaha", "omaha2"):
            for betting in ("nl", "pl"):
                for pid in ids:
                    current = host.evaluate("id=>state.players.find(p=>p.id===id).chips", pid)
                    if current != 1000:
                        host.evaluate("([target,delta])=>sendMessage({type:'adjust_stack',target,delta})", [pid, 1000-current])
                host.wait_for_function("state.players.every(p=>p.chips===1000)")
                host.evaluate("([variant,betting])=>sendMessage({type:'set_variant',variant,betting})", [variant, betting])
                host.wait_for_function("([v,b])=>state.variant===v && state.betting===b", arg=[variant, betting])
                host.locator("#game-toggle-btn").click()
                host.wait_for_function("state.hand_in_progress && state.auto_running")
                assert host.evaluate("state.players.every(p=>p.hole===null)")
                for _ in range(150):
                    state = host.evaluate("state")
                    if not state["hand_in_progress"]:
                        break
                    actor_id = state["to_act"]
                    if actor_id:
                        actor = pages[ids.index(actor_id)]
                        actor.wait_for_function("priv && priv.your_turn", polling=100)
                        previous = host.evaluate("JSON.stringify(state)")
                        actor.evaluate("() => {const l=priv.legal; sendAction(l.can_raise?'raise':l.can_check?'check':'call',l.can_raise?l.max_raise_to:0)}")
                        host.wait_for_function("previous=>JSON.stringify(state)!==previous", arg=previous, polling=100)
                    else:
                        host.wait_for_timeout(200)
                else:
                    raise AssertionError("Hand did not complete")
                host.evaluate("sendMessage({type:'pause'})")
                host.wait_for_function("!state.auto_running")
                assert host.evaluate("state.players.reduce((n,p)=>n+p.chips,0)") == 2000
                print("PASS live game", variant, betting, flush=True)
        host.locator("#replay-btn").click()
        host.wait_for_function("replayList.length===6")
        host.locator('[data-close="replay-modal"]').click()
        with host.expect_popup() as opening:
            host.locator("#trainer-link").click()
        trainer = opening.value
        trainer.wait_for_selector("#stage-list .stage")
        assert trainer.locator(".stage").count() == 8
        trainer.evaluate("prog.settings.sound=false;startStage('open','all')")
        trainer.evaluate("answer(Ranges.bestActions(q.rule)[0])")
        trainer.locator("#play-library-open").click()
        trainer.select_option("#library-spot", "open/openraising_40_100bb/mp")
        trainer.locator("#library-stack").hover()
        trainer.mouse.wheel(0, 100)
        trainer.wait_for_function("$('library-stack').selectedIndex===1")
        assert trainer.locator("#library-spot option").count() == 177
        assert host.evaluate("ws.readyState") == 1
        trainer.screenshot(path=str(out / "cf_trainer.png"))
        trainer.close()
        host.screenshot(path=str(out / "cf_game.png"))
        stats = contexts[0].new_page()
        stats.goto(base + "/stats?room=" + room)
        stats.wait_for_selector("#content", state="visible")
        assert stats.evaluate("ROOM") == room
        assert stats.evaluate("PLAYERS.length") == 2
        assert not errors, errors
        (out / "cf_validation.json").write_text(json.dumps({"url": base, "room": room, "test_hands": 6,
                                                           "errors": errors}, indent=2), encoding="utf-8")
        print("PASS desktop/phone, six game modes, six D1 replays, trainer/library/wheel, WebSocket preserved", flush=True)
    finally:
        for page in pages:
            try:
                page.evaluate("sendMessage({type:'leave'})")
            except Exception:
                pass
        for ctx in contexts:
            ctx.close()
        browser.close()
