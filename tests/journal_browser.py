"""Drive the hand journal on a phone viewport against a running worker (local dev by default).

Usage: python tests/journal_browser.py [BASE_URL] [SHOT_DIR]
Uses a throwaway random code, records two hands (one with full actions), checks list, stats,
replay, edit, offline queueing and delete, then removes everything it created.
"""
import json
import secrets
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799").rstrip("/")
shots = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent / "shots"
shots.mkdir(parents=True, exist_ok=True)
code = "verify-" + secrets.token_hex(5)


def pick(page, group, index, cards):
    """Tap a slot, then rank and suit for each card; the sheet advances by itself."""
    page.locator(f'[data-act="pick"][data-g="{group}"][data-i="{index}"]').click()
    for card in cards:
        page.locator(f'#sheet [data-act="rank"][data-v="{card[0]}"]').click()
        page.locator(f'#sheet [data-act="suit"][data-v="{card[1]}"]').click()
    if page.locator("#sheet").is_visible():
        page.locator('#sheet [data-act="close-sheet"]').click()


def chip(page, act, value):
    page.locator(f'[data-act="{act}"][data-v="{value}"]').first.click()


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # The offline step makes the browser log one failed fetch on purpose; anything else is a real error.
    page.on("console", lambda m: m.type == "error" and "ERR_INTERNET_DISCONNECTED" not in m.text and errors.append(m.text))
    page.goto(base + "/journal/")
    page.screenshot(path=str(shots / "journal_code.png"))
    page.fill("#code-input", code)
    page.locator("#code-form button[type=submit]").click()
    page.wait_for_selector(".empty-state")
    page.screenshot(path=str(shots / "journal_empty.png"))

    # Hand 1: BTN vs BB single raised pot with actions through the turn.
    page.locator('.tabs a[data-tab="new"]').click()
    chip(page, "size", "6")
    page.fill('[data-f="sb"]', "1")
    page.fill('[data-f="bb"]', "2")
    page.fill('[data-f="session"]', "검증 세션")
    chip(page, "hero-pos", "BTN")
    chip(page, "stack-bb", "200")
    assert page.locator('[data-f="hero.stack"]').input_value() == "400"
    pick(page, "hero", 0, ["As", "Kd"])
    pick(page, "board", 0, ["Kh", "7c", "2d", "9s"])
    page.locator('[data-act="add-villain"]').click()
    page.locator('[data-act="vil-pos"][data-i="0"][data-v="BB"]').click()
    page.fill('[data-f="villains.0.stack"]', "250")
    page.wait_for_timeout(400)  # the action panel redraws once typing settles
    # Preflop: next actor is BTN (only listed players act); raise to 3bb, BB calls.
    assert "다음 차례 BTN" in page.locator(".builder").inner_text()
    chip(page, "do", "raise")
    chip(page, "preset", "6")
    page.locator('[data-act="confirm-bet"]').click()
    assert "다음 차례 BB" in page.locator(".builder").inner_text()
    chip(page, "do", "call")
    assert "베팅이 끝났어요" in page.locator(".actions").inner_text()
    assert "13" in page.locator(".pot-line").inner_text()  # 6 + 6 + dead SB 1
    page.locator('.actions [data-act="street"][data-v="flop"]').last.click()
    assert "다음 차례 BB" in page.locator(".builder").inner_text()
    chip(page, "do", "check")
    chip(page, "do", "bet")
    page.fill("#amount", "8")
    page.locator('[data-act="confirm-bet"]').click()
    chip(page, "do", "call")
    page.locator('.actions [data-act="street"][data-v="turn"]').last.click()
    chip(page, "do", "check")
    chip(page, "do", "bet")
    page.fill("#amount", "20")
    page.locator('[data-act="confirm-bet"]').click()
    chip(page, "do", "fold")
    assert "한 명만 남아" in page.locator(".actions").inner_text()
    guess = page.locator('[data-act="use-guess"]')
    assert guess.inner_text().startswith("+15"), guess.inner_text()  # pot 49 - invested 34
    guess.click()
    chip(page, "tag", "밸류")
    page.fill('[data-f="note"]', "턴 벳에 폴드")
    page.screenshot(path=str(shots / "journal_form.png"), full_page=True)
    page.locator(".savebar button[type=submit]").click()
    page.wait_for_selector(".replay")
    page.wait_for_function("() => document.querySelector('#sync-state').textContent === '저장됨'")
    page.screenshot(path=str(shots / "journal_detail.png"), full_page=True)
    for _ in range(4):
        page.locator('.replay [data-act="rp"].primary').click()
    assert "BB" in page.locator(".rp-caption").inner_text()
    page.screenshot(path=str(shots / "journal_replay.png"))

    # Hand 2 from the same table, no actions, a loss; saved while offline then synced.
    page.locator('[data-act="same-table"]').click()
    assert page.locator('[data-f="session"]').input_value() == "검증 세션"
    chip(page, "hero-pos", "BB")
    pick(page, "hero", 0, ["Qs", "Js"])
    chip(page, "sign", "-1")
    page.fill('[data-f="result"]', "30")
    context.set_offline(True)
    page.locator(".savebar button[type=submit]").click()
    page.wait_for_function("() => document.querySelector('#sync-state').textContent.includes('대기')")
    context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_function("() => document.querySelector('#sync-state').textContent === '저장됨'")

    page.locator('.tabs a[data-tab="list"]').click()
    page.wait_for_selector(".hand-list .hcard")
    assert page.locator(".hand-list .hcard").count() == 2
    stats = page.locator(".stat-grid").inner_text()
    # +15 chips (7.5bb) and -30 chips (-15bb) at 1/2
    assert "−15" in stats and "−7.5" in stats and "50%" in stats, stats
    page.screenshot(path=str(shots / "journal_list.png"), full_page=True)
    page.select_option('[data-filter="result"]', "loss")
    assert page.locator(".hand-list .hcard").count() == 1

    # Fresh browser with the same code sees the same server data (phone -> PC).
    other = browser.new_context(viewport={"width": 1280, "height": 900})
    pc = other.new_page()
    pc.goto(base + "/journal/")
    pc.fill("#code-input", code)
    pc.locator("#code-form button[type=submit]").click()
    pc.wait_for_function("() => document.querySelectorAll('.hand-list .hcard').length === 2")
    pc.screenshot(path=str(shots / "journal_desktop.png"), full_page=True)
    other.close()

    # Edit hand 2, then delete both.
    page.select_option('[data-filter="result"]', "")
    page.locator(".hand-list .hcard").nth(0).click()
    page.locator('a.btn[href^="#/edit/"]').click()
    page.fill('[data-f="note"]', "수정 확인")
    page.locator(".savebar button[type=submit]").click()
    page.wait_for_function("() => document.querySelector('#sync-state').textContent === '저장됨'")
    assert "수정 확인" in page.locator(".d-note").inner_text()
    page.on("dialog", lambda d: d.accept())
    for _ in range(2):
        page.locator('.tabs a[data-tab="list"]').click()
        page.locator(".hand-list .hcard").first.click()
        page.locator('[data-act="delete"]').click()
        page.wait_for_function("() => document.querySelector('#sync-state').textContent === '저장됨'")
    left = page.evaluate("""async c => (await (await fetch('/journal/api/hands', {headers: {'X-Journal-Code': c}})).json()).hands.length""", code)
    assert left == 0, left
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors, errors
    browser.close()
print(json.dumps({"ok": True, "code": code, "shots": str(shots)}))
