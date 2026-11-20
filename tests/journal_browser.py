"""Drive the hand journal on a phone viewport against a running worker (local stand-in by default).

Usage: python tests/journal_browser.py [BASE_URL] [SHOT_DIR]
Creates a throwaway book (name printed at the end), records two hands (one with full actions), checks
the public directory, read-only viewing from another browser, password login on a second device,
offline queueing, edit and delete. The empty book itself stays; remove it from D1 after a production run.
"""
import json
import secrets
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799").rstrip("/")
shots = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent / "shots"
shots.mkdir(parents=True, exist_ok=True)
book = "vf-" + secrets.token_hex(4)
pin = "48" + secrets.token_hex(3)


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


def saved(page):
    page.wait_for_function("() => document.querySelector('#sync-state').textContent === '저장됨'")


def login(page, name, password, create=False, pin2=None):
    page.locator("#me-btn").click()
    page.wait_for_selector("#login-form")
    if create:
        chip(page, "login-mode", "create")
    page.fill("#login-name", name)
    page.fill("#login-pin", password)
    if create:
        page.fill("#login-pin2", pin2 or password)
    page.locator("#login-form button[type=submit]").click()


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # The offline step and the wrong-password checks log failed requests on purpose; anything else is a real error.
    expected = ("ERR_INTERNET_DISCONNECTED", "status of 401", "status of 409")
    page.on("console", lambda m: m.type == "error" and not any(x in m.text for x in expected) and errors.append(m.text))
    page.goto(base + "/journal/")
    page.wait_for_selector(".me-card")
    assert page.locator("#me-btn").inner_text() == "로그인"
    page.screenshot(path=str(shots / "journal_home.png"), full_page=True)

    # Create a book; the confirm field must match.
    login(page, book, pin, create=True, pin2=pin + "x")
    page.wait_for_selector("#toast:not([hidden])")
    assert "확인" in page.locator("#toast").inner_text()
    page.fill("#login-pin2", pin)
    page.screenshot(path=str(shots / "journal_login.png"))
    page.locator("#login-form button[type=submit]").click()
    page.wait_for_selector(".empty-state")
    assert page.locator("#me-btn").inner_text() == book
    assert book in page.locator(".book-head").inner_text()

    # Hand 1: BTN vs BB single raised pot with actions through the turn.
    page.locator('.tabs a[data-tab="new"]').click()
    assert book in page.locator(".form-owner").inner_text()
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
    assert "다음 차례 BTN" in page.locator(".builder").inner_text()
    chip(page, "do", "raise")
    chip(page, "preset", "6")
    page.locator('[data-act="confirm-bet"]').click()
    assert "다음 차례 BB" in page.locator(".builder").inner_text()
    chip(page, "do", "call")
    assert "베팅이 끝났어요" in page.locator(".actions").inner_text()
    assert "13" in page.locator(".pot-line").inner_text()  # 6 + 6 + dead SB 1
    page.locator('.actions [data-act="street"][data-v="flop"]').last.click()
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
    saved(page)
    page.screenshot(path=str(shots / "journal_detail.png"), full_page=True)
    for _ in range(4):
        page.locator('.replay [data-act="rp"].primary').click()
    assert "BB" in page.locator(".rp-caption").inner_text()

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
    saved(page)

    page.locator('.tabs a[data-tab="mine"]').click()
    page.wait_for_selector(".hand-list .hcard")
    assert page.locator(".hand-list .hcard").count() == 2
    stats = page.locator(".stat-grid").inner_text()
    assert "−15" in stats and "−7.5" in stats and "50%" in stats, stats  # +15 and -30 chips at 1/2
    page.screenshot(path=str(shots / "journal_list.png"), full_page=True)
    page.select_option('[data-filter="result"]', "loss")
    assert page.locator(".hand-list .hcard").count() == 1
    page.select_option('[data-filter="result"]', "")

    # The public directory lists the book with its hand count.
    page.locator('.tabs a[data-tab="home"]').click()
    card = page.locator(f'.bcard[href="#/b/{book}"]')
    card.wait_for()
    assert "2핸드" in card.inner_text() and "나" in card.inner_text(), card.inner_text()
    page.screenshot(path=str(shots / "journal_home_mine.png"), full_page=True)

    # A stranger without the password can read everything but cannot change it.
    other = browser.new_context(viewport={"width": 1280, "height": 900})
    viewer = other.new_page()
    viewer.goto(base + "/journal/")
    viewer.locator(f'.bcard[href="#/b/{book}"]').click()
    viewer.wait_for_function("() => document.querySelectorAll('.hand-list .hcard').length === 2")
    viewer.locator(".hand-list .hcard").first.click()
    viewer.wait_for_selector(".d-head")
    assert viewer.locator(".detail-actions").count() == 0
    viewer.screenshot(path=str(shots / "journal_viewer.png"), full_page=True)
    status = viewer.evaluate("""async b => (await fetch('/journal/api/books/' + b + '/hands/zzzzzzzz', {method: 'DELETE'})).status""", book)
    assert status == 401, status
    # Second device of the owner: wrong password is refused, the right one opens the same book.
    login(viewer, book, "wrong-pass")
    viewer.wait_for_selector("#toast:not([hidden])")
    assert "비밀번호" in viewer.locator("#toast").inner_text()
    viewer.fill("#login-pin", pin)
    viewer.locator("#login-form button[type=submit]").click()
    viewer.wait_for_function("() => document.querySelectorAll('.hand-list .hcard').length === 2")
    assert viewer.locator("#me-btn").inner_text() == book
    other.close()

    # Edit, then delete both from the phone.
    page.locator('.tabs a[data-tab="mine"]').click()
    page.locator(".hand-list .hcard").nth(0).click()
    page.locator('a.btn[href^="#/edit/"]').click()
    page.fill('[data-f="note"]', "수정 확인")
    page.locator(".savebar button[type=submit]").click()
    saved(page)
    assert "수정 확인" in page.locator(".d-note").inner_text()
    page.on("dialog", lambda d: d.accept())
    for _ in range(2):
        page.locator('.tabs a[data-tab="mine"]').click()
        page.locator(".hand-list .hcard").first.click()
        page.locator('[data-act="delete"]').click()
        saved(page)
    left = page.evaluate("""async b => (await (await fetch('/journal/api/books/' + b + '/hands')).json()).hands.length""", book)
    assert left == 0, left
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#me-btn").click()
    page.locator('[data-act="logout"]').click()
    page.wait_for_function("() => document.querySelector('#me-btn').textContent === '로그인'")
    assert not errors, errors
    browser.close()
print(json.dumps({"ok": True, "book": book, "shots": str(shots)}))
