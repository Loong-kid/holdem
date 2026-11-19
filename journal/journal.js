"use strict";
/* Hand journal: record live hands on a phone, review them anywhere.
   Data lives in D1 under a personal code; this page keeps a cache, the unsent queue and the open draft locally. */

const $ = (s, r = document) => r.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const POS = {
  2: ["SB", "BB"], 3: ["SB", "BB", "BTN"], 4: ["SB", "BB", "UTG", "BTN"], 5: ["SB", "BB", "UTG", "CO", "BTN"],
  6: ["SB", "BB", "UTG", "HJ", "CO", "BTN"], 7: ["SB", "BB", "UTG", "UTG+1", "HJ", "CO", "BTN"],
  8: ["SB", "BB", "UTG", "UTG+1", "LJ", "HJ", "CO", "BTN"],
  9: ["SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"],
};
const RANKS = "AKQJT98765432";
const SUITS = [["s", "♠"], ["h", "♥"], ["d", "♦"], ["c", "♣"]];
const SUIT_SYM = Object.fromEntries(SUITS);
const STREETS = [["pre", "프리플랍"], ["flop", "플랍"], ["turn", "턴"], ["river", "리버"]];
const STREET_NAME = Object.fromEntries(STREETS);
const BOARD_COUNT = { pre: 0, flop: 3, turn: 4, river: 5 };
const PRESET_TAGS = ["밸류", "블러프", "히어로콜", "쿨러", "배드빗", "실수", "3벳팟", "멀티웨이"];
const KEY = { code: "journal.code", cache: "journal.cache", outbox: "journal.outbox", draft: "journal.draft", prefs: "journal.prefs" };

function load(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } }
function store(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: the page still works for this visit */ } }
function drop(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } }

let code = load(KEY.code, "");
let hands = load(KEY.cache, []);
let outbox = load(KEY.outbox, {});
let draft = null;          // the hand being entered or edited
let editing = null;        // id when editing an existing hand
let ui = { sign: 1, actStreet: "pre", pending: null, amount: "" };
let filters = { period: "all", session: "", pos: "", tag: "", result: "" };
let picker = null;
let replayIndex = 0;
let syncing = false;
let lastError = "";

/* ---------- small helpers ---------- */
const today = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); };
const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
const num = (v) => { if (v === "" || v == null) return null; const n = Number(String(v).replace(/,/g, "")); return Number.isFinite(n) ? n : null; };
const fmt = (n) => { if (n == null || !Number.isFinite(n)) return "?"; const r = Math.round(n * 100) / 100; return r.toLocaleString("ko-KR", { maximumFractionDigits: 2 }); };
const signed = (n) => (n > 0 ? "+" : n < 0 ? "−" : "") + fmt(Math.abs(n));
const bbOf = (n, h) => (n == null || !(h.bb > 0) ? null : n / h.bb);
const clone = (v) => JSON.parse(JSON.stringify(v));

function cardHTML(card, cls = "") {
  if (!card) return `<span class="pc empty ${cls}"></span>`;
  const red = card[1] === "h" || card[1] === "d";
  return `<span class="pc ${red ? "red" : ""} ${cls}"><b>${card[0] === "T" ? "10" : card[0]}</b><i>${SUIT_SYM[card[1]]}</i></span>`;
}
const cardsText = (cards) => (cards || []).filter(Boolean).map((c) => cardHTML(c, "mini")).join("");

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 2600);
}

/* ---------- action engine (shared by the builder and the replay) ---------- */
function order(n, street) {
  const p = POS[n];
  if (street === "pre") return n === 2 ? ["SB", "BB"] : [...p.slice(2), "SB", "BB"];
  return n === 2 ? ["BB", "SB"] : p.slice();
}

function players(h) {
  const list = [{ pos: h.hero.pos, stack: h.hero.stack, hero: true, cards: h.hero.cards }]
    .concat(h.villains.map((v) => ({ pos: v.pos, stack: v.stack, hero: false, cards: v.cards })))
    .filter((p) => p.pos);
  const o = order(h.table_size, "pre");
  return list.sort((a, b) => o.indexOf(a.pos) - o.indexOf(b.pos));
}

/* Runs the hand. Returns frames for the replay plus the live state after `stopStreet`'s recorded actions. */
function simulate(h, stopStreet = "river") {
  const st = {};
  for (const p of players(h)) {
    st[p.pos] = { pos: p.pos, hero: p.hero, cards: p.cards, start: p.stack, left: p.stack == null ? Infinity : +p.stack,
      bet: 0, inv: 0, folded: false, allin: false, last: "" };
  }
  let dead = 0;
  const put = (s, amount) => { const a = Math.min(amount, s.left); s.left -= a; s.bet += a; s.inv += a; if (s.left <= 0) s.allin = true; return a; };
  const sb = +h.sb || 0, bb = +h.bb || 0;
  if (st.SB) { put(st.SB, sb); st.SB.last = "SB " + fmt(sb); } else dead += sb;
  if (st.BB) { put(st.BB, bb); st.BB.last = "BB " + fmt(bb); } else dead += bb;
  let cur = bb;
  let acted = new Set();
  const frames = [];
  const pot = () => dead + Object.values(st).reduce((t, s) => t + s.inv, 0);
  const snap = (street, actor, caption) => frames.push({
    street, actor, caption, pot: pot(), board: BOARD_COUNT[street],
    seats: Object.values(st).map((s) => ({ pos: s.pos, hero: s.hero, left: s.left, bet: s.bet, folded: s.folded, allin: s.allin, last: s.last })),
  });
  snap("pre", null, "블라인드 " + fmt(sb) + "/" + fmt(bb));
  let live = null;
  for (const [street] of STREETS) {
    if (street !== "pre") {
      const acts = h.actions[street] || [];
      const reached = h.board.slice(0, BOARD_COUNT[street]).every(Boolean);
      if (!acts.length && !reached && street !== stopStreet) break;
      for (const s of Object.values(st)) { s.bet = 0; if (!s.folded) s.last = ""; }
      cur = 0;
      acted = new Set();
      const shown = h.board.slice(BOARD_COUNT[street] === 3 ? 0 : BOARD_COUNT[street] - 1, BOARD_COUNT[street]).filter(Boolean);
      snap(street, null, STREET_NAME[street] + (shown.length ? " " + shown.map((c) => (c[0] === "T" ? "10" : c[0]) + SUIT_SYM[c[1]]).join(" ") : ""));
    }
    for (const a of h.actions[street] || []) {
      const s = st[a.pos];
      if (!s) continue;
      let text;
      if (a.act === "fold") { s.folded = true; text = "폴드"; }
      else if (a.act === "check") { text = "체크"; }
      else if (a.act === "call") { put(s, Math.max(0, cur - s.bet)); text = "콜 " + fmt(s.bet); }
      else if (a.act === "allin") {
        put(s, s.left);
        if (s.bet > cur) { cur = s.bet; acted = new Set(); } // a short all-in call does not reopen the action
        text = "올인 " + fmt(s.bet);
      }
      else { put(s, Math.max(0, a.to - s.bet)); cur = Math.max(cur, s.bet); text = (a.act === "bet" ? "벳 " : "레이즈 ") + fmt(s.bet); acted = new Set(); }
      s.last = text;
      acted.add(a.pos);
      snap(street, a.pos, a.pos + " " + text);
    }
    if (street === stopStreet) { live = { st, cur, acted, pot: pot(), street }; break; }
  }
  return { frames, live: live || { st, cur, acted, pot: pot(), street: stopStreet } };
}

function nextActor(h, street, live) {
  const st = live.st;
  const inHand = Object.values(st).filter((s) => !s.folded);
  if (inHand.length <= 1) return null;
  const canAct = inHand.filter((s) => !s.allin);
  if (!canAct.length || (canAct.length === 1 && canAct[0].bet >= live.cur && live.acted.has(canAct[0].pos))) return null;
  const full = order(h.table_size, street);
  const acts = h.actions[street] || [];
  const start = acts.length ? full.indexOf(acts[acts.length - 1].pos) + 1 : 0;
  for (let i = 0; i < full.length; i++) {
    const pos = full[(start + i) % full.length];
    const s = st[pos];
    if (s && !s.folded && !s.allin && (!live.acted.has(pos) || s.bet < live.cur)) return pos;
  }
  return null;
}

function handOver(live) { return Object.values(live.st).filter((s) => !s.folded).length <= 1; }

/* ---------- storage + sync ---------- */
function persist() { store(KEY.cache, hands); store(KEY.outbox, outbox); }

function visibleHands() {
  const map = new Map(hands.map((h) => [h.id, h]));
  for (const [id, job] of Object.entries(outbox)) {
    if (job.op === "delete") map.delete(id); else map.set(id, job.hand);
  }
  return [...map.values()].sort((a, b) => (b.played_at + (b.updated_at || b.created_at || "")).localeCompare(a.played_at + (a.updated_at || a.created_at || "")));
}

async function api(method, path, body) {
  const response = await fetch("/journal/api" + path, {
    method, headers: { "Content-Type": "application/json", "X-Journal-Code": code },
    body: body ? JSON.stringify(body) : undefined, cache: "no-store",
  });
  let data = {};
  try { data = await response.json(); } catch { /* empty */ }
  if (!response.ok) { const e = new Error(data.error || "서버 오류 " + response.status); e.status = response.status; throw e; }
  return data;
}

function renderSync() {
  const el = $("#sync-state");
  const waiting = Object.keys(outbox).length;
  el.className = "sync-state" + (waiting ? " wait" : "") + (lastError ? " err" : "");
  el.textContent = !code ? "" : syncing ? "올리는 중" : waiting ? `기기에 ${waiting}개 대기` : "저장됨";
  el.title = lastError || "";
}

async function sync() {
  if (!code || syncing) return;
  syncing = true; renderSync();
  try {
    for (const [id, job] of Object.entries(outbox)) {
      try {
        if (job.op === "delete") {
          await api("DELETE", "/hands/" + id);
          hands = hands.filter((h) => h.id !== id);
        } else {
          const result = await api("PUT", "/hands/" + id, job.hand);
          hands = [result.hand, ...hands.filter((h) => h.id !== id)];
        }
        delete outbox[id];
        persist();
        lastError = "";
      } catch (error) {
        if (error.status === 400 || error.status === 413) { job.error = error.message; persist(); lastError = error.message; continue; }
        if (error.status === 401) { lastError = error.message; break; }
        lastError = "연결이 안 돼 기기에 보관 중"; break;
      }
    }
    if (!Object.keys(outbox).length) {
      const data = await api("GET", "/hands");
      hands = data.hands;
      persist();
      lastError = "";
    }
  } catch (error) {
    lastError = error.status ? error.message : "연결이 안 돼 기기에 보관 중";
  } finally {
    syncing = false; renderSync();
    if (/^#\/(list|hand)/.test(location.hash)) route();
  }
}

/* ---------- drafts ---------- */
function prefs() { return load(KEY.prefs, {}); }

function newDraft() {
  const p = prefs();
  return {
    id: newId(), played_at: today(), session: p.session || "", table_size: p.table_size || 6,
    sb: p.sb ?? 1, bb: p.bb ?? 2,
    hero: { pos: "", stack: p.stack ?? null, cards: [null, null] },
    villains: [], board: [null, null, null, null, null],
    actions: { pre: [], flop: [], turn: [], river: [] }, result: null, tags: [], note: "",
  };
}

function saveDraft() { if (!editing) store(KEY.draft, draft); }

function pruneActions() {
  const alive = new Set(players(draft).map((p) => p.pos));
  for (const [street] of STREETS) draft.actions[street] = (draft.actions[street] || []).filter((a) => alive.has(a.pos));
}

/* ---------- views ---------- */
function setTab(name) {
  for (const a of document.querySelectorAll("#tabs a")) a.classList.toggle("on", a.dataset.tab === name);
}

function route() {
  const view = $("#view");
  const hash = location.hash || "#/new";
  $("#tabs").hidden = !code;
  $("#code-btn").hidden = !code;
  closeSheet();
  if (!code) { setTab(""); view.innerHTML = codeView(); return; }
  let m;
  if ((m = hash.match(/^#\/edit\/(.+)$/))) {
    const hand = visibleHands().find((h) => h.id === m[1]);
    if (!hand) { location.hash = "#/list"; return; }
    if (editing !== hand.id) { editing = hand.id; draft = clone(hand); ui = { sign: (hand.result ?? 0) < 0 ? -1 : 1, actStreet: "pre", pending: null, amount: "" }; }
    setTab("new"); renderForm(); return;
  }
  if ((m = hash.match(/^#\/hand\/(.+)$/))) { setTab("list"); view.innerHTML = detailView(m[1]); return; }
  if (hash.startsWith("#/list")) { setTab("list"); view.innerHTML = listView(); return; }
  if (editing) { editing = null; draft = null; }
  if (!draft) draft = load(KEY.draft, null) || newDraft();
  setTab("new");
  renderForm();
}

function codeView() {
  return `<section class="panel codebox">
    <h2>기록 코드</h2>
    <p class="lead">기록 코드는 내 기록장의 열쇠예요. 폰과 PC에서 같은 코드를 넣으면 같은 기록이 보여요.</p>
    <form id="code-form" class="code-form" autocomplete="off">
      <label for="code-input">코드 입력</label>
      <input id="code-input" name="code" inputmode="text" autocapitalize="none" spellcheck="false"
        placeholder="예: river-7k2m-ace" minlength="6" maxlength="64" required />
      <button class="btn primary" type="submit">기록장 열기</button>
    </form>
    <div class="divider"><span>처음이라면</span></div>
    <button class="btn" type="button" data-act="gen-code">새 코드 만들기</button>
    <p class="fine">영문 소문자·숫자·하이픈 6자 이상. 코드를 잊으면 기록을 찾을 수 없으니 꼭 따로 적어 두세요. 다른 사람이 알면 그 사람도 볼 수 있어요.</p>
  </section>`;
}

/* --- form --- */
function chip(label, attrs, on, disabled) {
  return `<button type="button" class="chip${on ? " on" : ""}" ${attrs} aria-pressed="${on ? "true" : "false"}"${disabled ? " disabled" : ""}>${label}</button>`;
}

function slot(card, attrs, big) {
  return `<button type="button" class="slot${big ? " big" : ""}${card ? "" : " empty"}" ${attrs}>${card ? cardHTML(card) : `<span class="plus">+</span>`}</button>`;
}

function renderForm() {
  const h = draft;
  pruneActions();
  const positions = order(h.table_size, "pre");
  const taken = new Set([h.hero.pos, ...h.villains.map((v) => v.pos)].filter(Boolean));
  const sessions = [...new Set(visibleHands().map((x) => x.session).filter(Boolean))].slice(0, 12);
  const stackBB = bbOf(num(h.hero.stack), h);
  const html = `
  <form id="hand-form" class="form" autocomplete="off" novalidate>
    ${editing ? `<div class="edit-banner">핸드 수정 중 <a href="#/hand/${esc(editing)}">취소</a></div>` : ""}
    <section class="panel">
      <h2>테이블</h2>
      <div class="row2">
        <label class="field"><span>날짜</span><input type="date" data-f="played_at" value="${esc(h.played_at)}" /></label>
        <label class="field"><span>세션</span><input list="session-list" data-f="session" maxlength="60" placeholder="예: 금요일 홈게임" value="${esc(h.session)}" /></label>
      </div>
      <datalist id="session-list">${sessions.map((s) => `<option value="${esc(s)}">`).join("")}</datalist>
      <div class="label">인원</div>
      <div class="chips">${[2, 3, 4, 5, 6, 7, 8, 9].map((n) => chip(n + "인", `data-act="size" data-v="${n}"`, h.table_size === n)).join("")}</div>
      <div class="row2">
        <label class="field"><span>스몰 블라인드</span><input inputmode="decimal" data-f="sb" value="${esc(h.sb ?? "")}" /></label>
        <label class="field"><span>빅 블라인드</span><input inputmode="decimal" data-f="bb" value="${esc(h.bb ?? "")}" /></label>
      </div>
    </section>

    <section class="panel">
      <h2>나</h2>
      <div class="label">포지션</div>
      <div class="chips">${positions.map((p) => chip(p, `data-act="hero-pos" data-v="${p}"`, h.hero.pos === p, taken.has(p) && h.hero.pos !== p)).join("")}</div>
      <label class="field"><span>시작 스택 <em id="stack-bb">${stackBB != null ? fmt(stackBB) + "bb" : ""}</em></span>
        <input inputmode="decimal" data-f="hero.stack" placeholder="칩 수" value="${esc(h.hero.stack ?? "")}" /></label>
      <div class="chips quick">${[50, 100, 200].map((n) => chip(n + "bb", `data-act="stack-bb" data-v="${n}"`, stackBB === n)).join("")}</div>
      <div class="label">내 패</div>
      <div class="slots">${h.hero.cards.map((c, i) => slot(c, `data-act="pick" data-g="hero" data-i="${i}"`, true)).join("")}</div>
    </section>

    <section class="panel">
      <h2>보드</h2>
      <div class="board-slots">
        <div class="bgroup"><span>플랍</span><div class="slots">${[0, 1, 2].map((i) => slot(h.board[i], `data-act="pick" data-g="board" data-i="${i}"`)).join("")}</div></div>
        <div class="bgroup"><span>턴</span><div class="slots">${slot(h.board[3], `data-act="pick" data-g="board" data-i="3"`)}</div></div>
        <div class="bgroup"><span>리버</span><div class="slots">${slot(h.board[4], `data-act="pick" data-g="board" data-i="4"`)}</div></div>
      </div>
    </section>

    <section class="panel">
      <h2>상대 <small>선택</small></h2>
      ${h.villains.map((v, vi) => villainBlock(v, vi, positions, taken)).join("")}
      ${h.villains.length < h.table_size - 1 ? `<button type="button" class="btn ghost wide" data-act="add-villain">상대 추가</button>` : ""}
    </section>

    ${actionsPanel()}

    <section class="panel">
      <h2>결과</h2>
      <div class="seg" role="group" aria-label="이김 또는 짐">
        ${chip("이김", `data-act="sign" data-v="1"`, ui.sign === 1)}${chip("짐", `data-act="sign" data-v="-1"`, ui.sign === -1)}
      </div>
      <label class="field"><span>손익 (칩) <em id="result-bb">${resultBBText()}</em></span>
        <input inputmode="decimal" data-f="result" placeholder="예: 85" value="${esc(h.result == null ? "" : Math.abs(h.result))}" /></label>
      ${resultHint()}
    </section>

    <section class="panel">
      <h2>태그·메모</h2>
      <div class="chips">${[...new Set([...PRESET_TAGS, ...h.tags])].map((t) => chip(esc(t), `data-act="tag" data-v="${esc(t)}"`, h.tags.includes(t))).join("")}</div>
      <div class="tag-add"><input id="tag-input" maxlength="20" placeholder="새 태그" enterkeyhint="done" /><button type="button" class="btn small" data-act="add-tag">추가</button></div>
      <label class="field"><span>메모</span><textarea data-f="note" rows="3" maxlength="2000" placeholder="상대 성향, 생각했던 것">${esc(h.note)}</textarea></label>
    </section>

    <div class="savebar">
      ${editing ? `<button type="button" class="btn" data-act="cancel-edit">취소</button>` : `<button type="button" class="btn" data-act="reset">새로 쓰기</button>`}
      <button type="submit" class="btn primary">${editing ? "수정 저장" : "저장"}</button>
    </div>
  </form>`;
  $("#view").innerHTML = html;
}

function villainBlock(v, vi, positions, taken) {
  return `<div class="villain">
    <div class="villain-head"><span>상대 ${vi + 1}</span><button type="button" class="link danger" data-act="del-villain" data-i="${vi}">빼기</button></div>
    <div class="chips">${positions.map((p) => chip(p, `data-act="vil-pos" data-i="${vi}" data-v="${p}"`, v.pos === p, taken.has(p) && v.pos !== p)).join("")}</div>
    <div class="row2">
      <label class="field"><span>스택</span><input inputmode="decimal" data-f="villains.${vi}.stack" placeholder="모르면 비움" value="${esc(v.stack ?? "")}" /></label>
      <label class="field"><span>특징</span><input data-f="villains.${vi}.note" maxlength="60" placeholder="예: 루즈" value="${esc(v.note || "")}" /></label>
    </div>
    <div class="label">보여준 패</div>
    <div class="slots">${[0, 1].map((i) => slot((v.cards || [])[i] || null, `data-act="pick" data-g="v${vi}" data-i="${i}"`)).join("")}</div>
  </div>`;
}

function actionsPanel() {
  const h = draft;
  if (!h.hero.pos) return `<section class="panel"><h2>액션 <small>선택</small></h2><p class="muted">내 포지션을 고르면 액션을 넣을 수 있어요.</p></section>`;
  const street = ui.actStreet;
  const { live } = simulate(h, street);
  const acts = h.actions[street] || [];
  const actor = nextActor(h, street, live);
  const tabs = STREETS.map(([k, name]) => {
    const count = (h.actions[k] || []).length;
    return chip(name + (count ? ` <b>${count}</b>` : ""), `data-act="street" data-v="${k}"`, street === k);
  }).join("");
  const log = acts.length ? `<ol class="alog">${acts.map((a, i) => {
    const { frames } = simulate({ ...h, actions: { ...h.actions, [street]: acts.slice(0, i + 1) } }, street);
    return `<li><span class="pos-badge${a.pos === h.hero.pos ? " me" : ""}">${a.pos}</span>${esc(frames[frames.length - 1].caption.slice(a.pos.length + 1))}</li>`;
  }).join("")}</ol>` : `<p class="muted">${street === "pre" ? "블라인드는 자동으로 들어가요. 첫 액션부터 넣어 주세요." : "이 스트리트 액션이 아직 없어요."}</p>`;
  let builder = "";
  if (handOver(live)) builder = `<p class="note-ok">한 명만 남아 핸드가 끝났어요.</p>`;
  else if (!actor) {
    const nextStreet = STREETS[STREETS.findIndex(([k]) => k === street) + 1];
    builder = `<p class="note-ok">${STREET_NAME[street]} 베팅이 끝났어요.</p>` +
      (nextStreet ? `<button type="button" class="btn wide" data-act="street" data-v="${nextStreet[0]}">${nextStreet[1]}로 넘어가기</button>` : "");
  } else builder = actionBuilder(h, street, live, actor);
  return `<section class="panel actions">
    <h2>액션 <small>선택 · 넣으면 재생 가능</small></h2>
    <div class="chips tabs-row">${tabs}</div>
    <div class="pot-line">팟 <b>${fmt(live.pot)}</b>${h.bb > 0 ? ` <em>${fmt(live.pot / h.bb)}bb</em>` : ""}</div>
    ${log}
    ${acts.length ? `<button type="button" class="link" data-act="undo">마지막 액션 지우기</button>` : ""}
    ${builder}
  </section>`;
}

function actionBuilder(h, street, live, actor) {
  const ps = players(h).map((p) => p.pos).filter((p) => !live.st[p].folded && !live.st[p].allin);
  const chosen = ui.pending?.pos && ps.includes(ui.pending.pos) ? ui.pending.pos : actor;
  const s = live.st[chosen];
  const toCall = Math.max(0, live.cur - s.bet);
  const left = s.left;
  const buttons = [];
  if (toCall > 0) buttons.push(chip("폴드", `data-act="do" data-v="fold"`));
  if (toCall <= 0) buttons.push(chip("체크", `data-act="do" data-v="check"`));
  else buttons.push(chip("콜 " + fmt(Math.min(toCall, left) + s.bet), `data-act="do" data-v="call"`));
  if (left > toCall) buttons.push(chip(live.cur > 0 ? "레이즈" : "벳", `data-act="do" data-v="${live.cur > 0 ? "raise" : "bet"}"`, ui.pending?.act === "raise" || ui.pending?.act === "bet"));
  if (Number.isFinite(left) && left > 0) buttons.push(chip("올인 " + fmt(left + s.bet), `data-act="do" data-v="allin"`));
  let sizing = "";
  if (ui.pending && (ui.pending.act === "bet" || ui.pending.act === "raise") && ui.pending.pos === chosen) {
    const bb = +h.bb || 1;
    const potNow = live.pot;
    const presets = [];
    if (street === "pre") {
      if (live.cur <= bb) [2.5, 3, 4].forEach((x) => presets.push([x + "bb", x * bb]));
      else [3, 4].forEach((x) => presets.push([x + "배", x * live.cur]));
    } else if (live.cur === 0) {
      [[1 / 3, "⅓"], [1 / 2, "½"], [2 / 3, "⅔"], [1, "팟"]].forEach(([f, l]) => presets.push([l, Math.round(f * potNow * 100) / 100]));
    } else {
      [2.5, 3].forEach((x) => presets.push([x + "배", x * live.cur]));
      presets.push(["팟", live.cur + (potNow + toCall)]);
    }
    sizing = `<div class="sizing">
      <div class="chips">${presets.map(([l, v]) => chip(l, `data-act="preset" data-v="${Math.round(v * 100) / 100}"`)).join("")}</div>
      <div class="amount-row"><input id="amount" inputmode="decimal" placeholder="총 금액" value="${esc(ui.amount)}" /><button type="button" class="btn primary" data-act="confirm-bet">넣기</button></div>
      <p class="fine">이번 스트리트에 낸 총액으로 넣어요 (예: 3에서 9로 레이즈면 9).</p>
    </div>`;
  }
  return `<div class="builder">
    <div class="label">누가 <em>다음 차례 ${actor}</em></div>
    <div class="chips">${ps.map((p) => chip(p + (p === h.hero.pos ? " 나" : ""), `data-act="actor" data-v="${p}"`, p === chosen)).join("")}</div>
    <div class="label">무엇을</div>
    <div class="chips act-row">${buttons.join("")}</div>
    ${sizing}
  </div>`;
}

function resultBBText() {
  const r = draft.result;
  const b = bbOf(r, draft);
  return r == null ? "" : signed(b) + "bb";
}

function resultHint() {
  const h = draft;
  if (!h.hero.pos) return "";
  const { live } = simulate(h, "river");
  const me = live.st[h.hero.pos];
  if (!me || !me.inv) return "";
  const others = Object.values(live.st).filter((s) => !s.hero && !s.folded).length;
  let guess = null;
  if (me.folded) guess = -me.inv;
  else if (others === 0 && Object.keys(live.st).length > 1) guess = live.pot - me.inv;
  return `<p class="fine">액션 기준 내 투입 ${fmt(me.inv)} · 팟 ${fmt(live.pot)}${guess != null ? ` <button type="button" class="link" data-act="use-guess" data-v="${guess}">${signed(guess)} 넣기</button>` : ""}</p>`;
}

/* --- card picker sheet --- */
function slotsFor(group) {
  if (group === "hero") return [0, 1].map((i) => ({ g: "hero", i }));
  if (group === "board") return [0, 1, 2, 3, 4].map((i) => ({ g: "board", i }));
  return [0, 1].map((i) => ({ g: group, i }));
}
function getCard(t) {
  if (t.g === "hero") return draft.hero.cards[t.i];
  if (t.g === "board") return draft.board[t.i];
  return (draft.villains[+t.g.slice(1)].cards || [])[t.i] || null;
}
function setCard(t, card) {
  if (t.g === "hero") draft.hero.cards[t.i] = card;
  else if (t.g === "board") draft.board[t.i] = card;
  else {
    const v = draft.villains[+t.g.slice(1)];
    v.cards = v.cards && v.cards.length === 2 ? v.cards : [null, null];
    v.cards[t.i] = card;
    if (!v.cards.some(Boolean)) v.cards = [];
  }
}
function usedCards(except) {
  const all = [...draft.hero.cards, ...draft.board, ...draft.villains.flatMap((v) => v.cards || [])].filter(Boolean);
  const mine = getCard(except);
  return new Set(all.filter((c) => c !== mine));
}
const groupName = (g) => (g === "hero" ? "내 패" : g === "board" ? "보드" : `상대 ${+g.slice(1) + 1} 패`);
const slotName = (t) => (t.g === "board" ? ["플랍 1", "플랍 2", "플랍 3", "턴", "리버"][t.i] : `${t.i + 1}번째`);

function openPicker(group, index) {
  picker = { targets: slotsFor(group), index, rank: null };
  renderSheet();
  $("#sheet").hidden = false;
  $("#sheet-backdrop").hidden = false;
  document.body.classList.add("sheet-open");
}
function closeSheet() {
  picker = null;
  $("#sheet").hidden = true;
  $("#sheet-backdrop").hidden = true;
  document.body.classList.remove("sheet-open");
}
function renderSheet() {
  const t = picker.targets[picker.index];
  const used = usedCards(t);
  const group = picker.targets.map((x, k) => `<button type="button" class="mini-slot${k === picker.index ? " cur" : ""}" data-act="jump" data-v="${k}">${getCard(x) ? cardHTML(getCard(x), "mini") : `<span class="ms-label">${slotName(x).replace("번째", "")}</span>`}</button>`).join("");
  const ranks = [...RANKS].map((r) => {
    const full = SUITS.every(([s]) => used.has(r + s));
    return `<button type="button" class="rank${picker.rank === r ? " on" : ""}" data-act="rank" data-v="${r}"${full ? " disabled" : ""}>${r === "T" ? "10" : r}</button>`;
  }).join("");
  const suits = SUITS.map(([s, sym]) => {
    const dis = !picker.rank || used.has(picker.rank + s);
    return `<button type="button" class="suit ${s === "h" || s === "d" ? "red" : ""}" data-act="suit" data-v="${s}"${dis ? " disabled" : ""} aria-label="${sym}">${sym}</button>`;
  }).join("");
  $("#sheet").innerHTML = `
    <div class="sheet-grip" aria-hidden="true"></div>
    <div class="sheet-head"><h2 id="sheet-title">${groupName(t.g)} · ${slotName(t)}</h2><button type="button" class="link" data-act="close-sheet">완료</button></div>
    <div class="mini-slots">${group}</div>
    <div class="rank-grid">${ranks}</div>
    <div class="suit-row">${suits}</div>
    <div class="sheet-foot"><button type="button" class="btn" data-act="clear-card">이 칸 비우기</button></div>`;
}

/* --- list + stats --- */
function filtered() {
  const now = today();
  const days = { "7": 7, "30": 30 }[filters.period];
  const since = days ? new Date(Date.parse(now) - (days - 1) * 864e5).toISOString().slice(0, 10) : null;
  return visibleHands().filter((h) =>
    (filters.period !== "today" || h.played_at === now) && (!since || h.played_at >= since) &&
    (!filters.session || h.session === filters.session) && (!filters.pos || h.hero.pos === filters.pos) &&
    (!filters.tag || (h.tags || []).includes(filters.tag)) &&
    (!filters.result || (filters.result === "win" ? (h.result ?? 0) > 0 : (h.result ?? 0) < 0)));
}

function listView() {
  const all = visibleHands();
  if (!all.length) return `<section class="panel empty-state"><h2>아직 기록이 없어요</h2><p class="muted">한 판 끝날 때마다 기록하기 탭에서 넣어 두면 여기서 모아 볼 수 있어요.</p><a class="btn primary" href="#/new">첫 핸드 기록하기</a></section>`;
  const rows = filtered();
  const sessions = [...new Set(all.map((h) => h.session).filter(Boolean))];
  const tags = [...new Set(all.flatMap((h) => h.tags || []))];
  const posList = ["UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN", "SB", "BB"].filter((p) => all.some((h) => h.hero.pos === p));
  const withResult = rows.filter((h) => h.result != null);
  const netBB = withResult.reduce((t, h) => t + (bbOf(h.result, h) || 0), 0);
  const net = withResult.reduce((t, h) => t + h.result, 0);
  const wins = withResult.filter((h) => h.result > 0).length;
  const byPos = posList.map((p) => {
    const xs = withResult.filter((h) => h.hero.pos === p);
    return { p, n: xs.length, bb: xs.reduce((t, h) => t + (bbOf(h.result, h) || 0), 0) };
  }).filter((x) => x.n);
  const maxAbs = Math.max(1, ...byPos.map((x) => Math.abs(x.bb)));
  const sel = (name, options, label) => `<label class="field compact"><span>${label}</span><select data-filter="${name}">${options.map(([v, t]) => `<option value="${esc(v)}"${filters[name] === v ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`;
  return `
  <section class="panel filters">
    <div class="filter-grid">
      ${sel("period", [["all", "전체 기간"], ["today", "오늘"], ["7", "최근 7일"], ["30", "최근 30일"]], "기간")}
      ${sel("session", [["", "모든 세션"], ...sessions.map((s) => [s, s])], "세션")}
      ${sel("pos", [["", "모든 포지션"], ...posList.map((p) => [p, p])], "포지션")}
      ${sel("result", [["", "승패 전체"], ["win", "이긴 핸드"], ["loss", "진 핸드"]], "결과")}
      ${tags.length ? sel("tag", [["", "모든 태그"], ...tags.map((t) => [t, t])], "태그") : ""}
    </div>
  </section>
  <section class="panel stats">
    <div class="stat-grid">
      <div><span>핸드</span><b>${rows.length}</b></div>
      <div><span>손익</span><b class="${net > 0 ? "pos" : net < 0 ? "neg" : ""}">${signed(net)}</b></div>
      <div><span>bb 합계</span><b class="${netBB > 0 ? "pos" : netBB < 0 ? "neg" : ""}">${signed(netBB)}</b></div>
      <div><span>이긴 비율</span><b>${withResult.length ? Math.round((wins / withResult.length) * 100) + "%" : "-"}</b></div>
    </div>
    ${byPos.length > 1 ? `<div class="pos-bars">${byPos.map((x) => `<div class="pb"><span class="pb-pos">${x.p}</span><span class="pb-track"><span class="pb-fill ${x.bb < 0 ? "neg" : "pos"}" style="width:${(Math.abs(x.bb) / maxAbs) * 50}%;${x.bb < 0 ? "right:50%" : "left:50%"}"></span></span><span class="pb-val">${signed(x.bb)}bb <em>${x.n}</em></span></div>`).join("")}</div>` : ""}
  </section>
  <ul class="hand-list">${rows.map(handCard).join("") || `<li class="muted center">조건에 맞는 핸드가 없어요.</li>`}</ul>`;
}

function handCard(h) {
  const r = h.result;
  const b = bbOf(r, h);
  const pending = outbox[h.id] ? `<span class="pending">${outbox[h.id].error ? "저장 실패" : "대기"}</span>` : "";
  return `<li><a class="hcard" href="#/hand/${esc(h.id)}">
    <div class="hc-main">
      <span class="pos-badge me">${esc(h.hero.pos)}</span>
      <span class="hc-cards">${cardsText(h.hero.cards) || `<span class="muted">패 없음</span>`}</span>
      <span class="hc-res ${r > 0 ? "pos" : r < 0 ? "neg" : ""}">${r == null ? "-" : signed(b) + "bb"}</span>
    </div>
    <div class="hc-board">${h.board.some(Boolean) ? cardsText(h.board.slice(0, 3)) + (h.board[3] ? `<span class="gap"></span>${cardsText([h.board[3]])}` : "") + (h.board[4] ? `<span class="gap"></span>${cardsText([h.board[4]])}` : "") : `<span class="muted">프리플랍에서 끝</span>`}</div>
    <div class="hc-meta">${esc(h.played_at.slice(5).replace("-", "."))} · ${h.table_size}인 ${fmt(h.sb)}/${fmt(h.bb)}${h.session ? " · " + esc(h.session) : ""}${(h.tags || []).length ? " · " + h.tags.map(esc).join(", ") : ""} ${pending}</div>
  </a></li>`;
}

/* --- detail + replay --- */
function detailView(id) {
  const h = visibleHands().find((x) => x.id === id);
  if (!h) return `<section class="panel"><p>핸드를 찾을 수 없어요.</p><a class="btn" href="#/list">목록으로</a></section>`;
  const r = h.result;
  const hasActions = STREETS.some(([k]) => (h.actions[k] || []).length);
  const job = outbox[h.id];
  return `
  <div class="detail-top"><a class="link" href="#/list">목록</a><span class="muted">${esc(h.played_at)}</span></div>
  ${job?.error ? `<p class="error-box">서버가 이 핸드를 받지 않았어요: ${esc(job.error)} 수정해서 다시 저장해 주세요.</p>` : ""}
  <section class="panel">
    <div class="d-head">
      <span class="pos-badge me big">${esc(h.hero.pos)}</span>
      <div class="d-cards">${h.hero.cards.map((c) => cardHTML(c, "lg")).join("")}</div>
      <div class="d-res ${r > 0 ? "pos" : r < 0 ? "neg" : ""}">${r == null ? "결과 없음" : `${signed(bbOf(r, h))}bb<small>${signed(r)}</small>`}</div>
    </div>
    <div class="d-meta">${h.table_size}인 · ${fmt(h.sb)}/${fmt(h.bb)}${h.hero.stack != null ? ` · 스택 ${fmt(h.hero.stack)} (${fmt(bbOf(h.hero.stack, h))}bb)` : ""}${h.session ? ` · ${esc(h.session)}` : ""}</div>
    <div class="d-board">${h.board.map((c) => cardHTML(c)).join("")}</div>
    ${h.villains.length ? `<ul class="d-vill">${h.villains.map((v) => `<li><span class="pos-badge">${esc(v.pos)}</span>${v.stack != null ? `<span>${fmt(v.stack)}</span>` : ""}${(v.cards || []).some(Boolean) ? `<span class="hc-cards">${cardsText(v.cards)}</span>` : ""}${v.note ? `<span class="muted">${esc(v.note)}</span>` : ""}</li>`).join("")}</ul>` : ""}
    ${(h.tags || []).length ? `<div class="d-tags">${h.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
    ${h.note ? `<p class="d-note">${esc(h.note)}</p>` : ""}
  </section>
  ${hasActions ? replayPanel(h) : `<p class="muted center">액션을 넣지 않은 핸드예요. 수정에서 액션을 넣으면 재생할 수 있어요.</p>`}
  <div class="detail-actions">
    <button type="button" class="btn primary wide-row" data-act="same-table" data-v="${esc(h.id)}">같은 테이블로 새 핸드</button>
    <a class="btn" href="#/edit/${esc(h.id)}">수정</a>
    <button type="button" class="btn danger" data-act="delete" data-v="${esc(h.id)}">삭제</button>
  </div>`;
}

function replayPanel(h) {
  const { frames } = simulate(h, "river");
  frames.push({ ...frames[frames.length - 1], caption: h.result == null ? "끝" : `결과 ${signed(bbOf(h.result, h))}bb`, actor: null, end: true });
  replayIndex = Math.min(replayIndex, frames.length - 1);
  const f = frames[replayIndex];
  const showAll = f.end;
  const seats = f.seats.map((s) => {
    const p = players(h).find((x) => x.pos === s.pos);
    const cards = s.hero || showAll ? (p.cards || []).filter(Boolean) : [];
    return `<li class="rp-seat${s.pos === f.actor ? " act" : ""}${s.folded ? " folded" : ""}">
      <span class="pos-badge${s.hero ? " me" : ""}">${s.pos}</span>
      <span class="rp-cards">${cards.map((c) => cardHTML(c, "mini")).join("")}</span>
      <span class="rp-last">${esc(s.last)}</span>
      <span class="rp-stack">${Number.isFinite(s.left) ? fmt(s.left) : "?"}</span>
    </li>`;
  }).join("");
  return `<section class="panel replay" data-hand="${esc(h.id)}">
    <h2>재생</h2>
    <div class="rp-board">${h.board.map((c, i) => (i < f.board ? cardHTML(c) : `<span class="pc back"></span>`)).join("")}</div>
    <div class="rp-pot">팟 <b>${fmt(f.pot)}</b> <em>${fmt(f.pot / h.bb)}bb</em> · ${STREET_NAME[f.street]}</div>
    <p class="rp-caption">${esc(f.caption)}</p>
    <ul class="rp-seats">${seats}</ul>
    <div class="rp-controls">
      <button type="button" class="btn small" data-act="rp" data-v="0" aria-label="처음">처음</button>
      <button type="button" class="btn small" data-act="rp" data-v="${Math.max(0, replayIndex - 1)}">이전</button>
      <input type="range" min="0" max="${frames.length - 1}" value="${replayIndex}" data-act="rp-range" aria-label="재생 위치" />
      <button type="button" class="btn small primary" data-act="rp" data-v="${Math.min(frames.length - 1, replayIndex + 1)}">다음</button>
    </div>
  </section>`;
}

/* ---------- events ---------- */
function setField(path, value) {
  const parts = path.split(".");
  let obj = draft;
  for (const p of parts.slice(0, -1)) obj = obj[p];
  const last = parts[parts.length - 1];
  const numeric = ["sb", "bb", "stack"].includes(last);
  if (last === "result") { const n = num(value); draft.result = n == null ? null : ui.sign * Math.abs(n); }
  else obj[last] = numeric ? num(value) : value;
}

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.f && draft) {
    setField(t.dataset.f, t.value);
    saveDraft();
    if (t.dataset.f === "hero.stack" || t.dataset.f === "bb") { const b = bbOf(num(draft.hero.stack), draft); const el = $("#stack-bb"); if (el) el.textContent = b != null ? fmt(b) + "bb" : ""; }
    if (t.dataset.f === "result" || t.dataset.f === "bb") { const el = $("#result-bb"); if (el) el.textContent = resultBBText(); }
    // Redraw the action panel shortly after typing stops. Doing it on blur would swap the button the
    // player is tapping out from under the finger and swallow that tap.
    if (t.dataset.f === "sb" || t.dataset.f === "bb" || t.dataset.f.endsWith("stack")) {
      clearTimeout(renderActions.timer);
      renderActions.timer = setTimeout(renderActions, 250);
    }
  }
  if (t.id === "amount") ui.amount = t.value;
  if (t.dataset.act === "rp-range") { replayIndex = +t.value; rerenderDetail(); }
});

document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.filter) { filters[t.dataset.filter] = t.value; $("#view").innerHTML = listView(); }
});

function renderActions() {
  const panel = $(".panel.actions") || [...document.querySelectorAll(".panel")].find((p) => p.querySelector("h2")?.textContent.startsWith("액션"));
  if (!panel || !draft) return;
  const fresh = document.createElement("div");
  fresh.innerHTML = actionsPanel();
  panel.replaceWith(fresh.firstElementChild);
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "tag-input") { e.preventDefault(); addTag(); }
  if (e.key === "Enter" && e.target.id === "amount") { e.preventDefault(); confirmBet(); }
  if (e.key === "Escape" && !$("#sheet").hidden) { const wasPicker = !!picker; closeSheet(); if (wasPicker && $("#hand-form")) renderForm(); }
});

document.addEventListener("submit", (e) => {
  e.preventDefault();
  if (e.target.id === "code-form") {
    const value = e.target.code.value.trim().toLowerCase();
    if (!/^[a-z0-9-]{6,64}$/.test(value)) { toast("영문 소문자·숫자·하이픈으로 6자 이상 넣어 주세요"); return; }
    setCode(value);
  }
  if (e.target.id === "hand-form") saveHand();
});

$("#sheet-backdrop").addEventListener("click", () => { const wasPicker = !!picker; closeSheet(); if (wasPicker && $("#hand-form")) renderForm(); });
$("#code-btn").addEventListener("click", () => openCodeSheet());

function setCode(value) {
  code = value;
  store(KEY.code, code);
  if (location.hash !== "#/list") location.hash = "#/list"; else route();
  sync();
}

function genCode() {
  const words = ["ace", "king", "river", "turn", "flop", "nuts", "suit", "chip", "deck", "pair", "set", "wheel"];
  const pick = () => words[crypto.getRandomValues(new Uint32Array(1))[0] % words.length];
  const tail = Array.from(crypto.getRandomValues(new Uint8Array(5)), (b) => "abcdefghjkmnpqrstuvwxyz23456789"[b % 31]).join("");
  return `${pick()}-${pick()}-${tail}`;
}

function openCodeSheet(generated) {
  picker = null;
  $("#sheet").innerHTML = `
    <div class="sheet-grip" aria-hidden="true"></div>
    <div class="sheet-head"><h2 id="sheet-title">${generated ? "새 기록 코드" : "내 기록 코드"}</h2><button type="button" class="link" data-act="close-sheet">닫기</button></div>
    <p class="code-show">${esc(generated || code)}</p>
    <p class="fine">${generated ? "이 코드를 지금 따로 적어 두세요. 잊으면 기록을 찾을 수 없어요." : "다른 기기에서 이 코드를 넣으면 같은 기록이 보여요."}</p>
    <div class="sheet-foot two">
      <button type="button" class="btn" data-act="copy-code" data-v="${esc(generated || code)}">복사</button>
      ${generated ? `<button type="button" class="btn primary" data-act="use-code" data-v="${esc(generated)}">이 코드로 시작</button>` : `<button type="button" class="btn danger" data-act="logout">이 기기에서 코드 지우기</button>`}
    </div>`;
  $("#sheet").hidden = false;
  $("#sheet-backdrop").hidden = false;
  document.body.classList.add("sheet-open");
}

function addTag() {
  const input = $("#tag-input");
  const value = input.value.trim().slice(0, 20);
  if (value && !draft.tags.includes(value) && draft.tags.length < 12) draft.tags.push(value);
  saveDraft();
  renderForm();
}

function confirmBet() {
  const amount = num(ui.amount);
  const { live } = simulate(draft, ui.actStreet);
  if (amount == null || amount <= live.cur) { toast(live.cur > 0 ? `${fmt(live.cur)}보다 큰 금액을 넣어 주세요` : "금액을 넣어 주세요"); return; }
  const s = live.st[ui.pending.pos];
  if (amount >= s.left + s.bet) draft.actions[ui.actStreet].push({ pos: ui.pending.pos, act: "allin" });
  else draft.actions[ui.actStreet].push({ pos: ui.pending.pos, act: ui.pending.act, to: amount });
  ui.pending = null; ui.amount = "";
  saveDraft(); renderForm();
}

function rerenderDetail() {
  const panel = $(".replay");
  if (!panel) return;
  const h = visibleHands().find((x) => x.id === panel.dataset.hand);
  const fresh = document.createElement("div");
  fresh.innerHTML = replayPanel(h);
  panel.replaceWith(fresh.firstElementChild);
}

function saveHand() {
  const h = clone(draft);
  if (!h.hero.pos) { toast("내 포지션을 골라 주세요"); return; }
  if (!(h.bb > 0)) { toast("빅 블라인드를 넣어 주세요"); return; }
  const flop = h.board.slice(0, 3);
  if (flop.some(Boolean) && !flop.every(Boolean)) { toast("플랍은 3장을 모두 넣어 주세요"); return; }
  if ((h.board[3] && !flop.every(Boolean)) || (h.board[4] && !h.board[3])) { toast("보드는 플랍·턴·리버 순서로 넣어 주세요"); return; }
  h.villains = h.villains.filter((v) => v.pos).map((v) => ({ ...v, cards: (v.cards || []).every(Boolean) && (v.cards || []).length === 2 ? v.cards : [] }));
  if (h.hero.cards.some(Boolean) && !h.hero.cards.every(Boolean)) { toast("내 패는 2장을 모두 넣어 주세요"); return; }
  h.created_at = h.created_at || new Date().toISOString();
  outbox[h.id] = { op: "put", hand: h };
  persist();
  store(KEY.prefs, { table_size: h.table_size, sb: h.sb, bb: h.bb, session: h.session, stack: h.hero.stack });
  const savedId = h.id;
  if (!editing) drop(KEY.draft);
  editing = null; draft = null; ui = { sign: 1, actStreet: "pre", pending: null, amount: "" };
  replayIndex = 0;
  location.hash = "#/hand/" + savedId;
  toast("저장했어요");
  sync();
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (!b || b.disabled || b.tagName === "INPUT") return;
  const act = b.dataset.act, v = b.dataset.v;
  switch (act) {
    case "gen-code": openCodeSheet(genCode()); break;
    case "use-code": closeSheet(); setCode(v); break;
    case "copy-code": navigator.clipboard?.writeText(v).then(() => toast("복사했어요"), () => toast("복사가 안 돼요. 직접 적어 두세요")); break;
    case "logout":
      if (Object.keys(outbox).length && !confirm("아직 서버에 못 올린 핸드가 있어요. 지우면 그 핸드는 사라져요. 계속할까요?")) break;
      code = ""; hands = []; outbox = {}; drop(KEY.code); drop(KEY.cache); drop(KEY.outbox); closeSheet(); route(); renderSync(); break;
    case "close-sheet": { const wasPicker = !!picker; closeSheet(); if (wasPicker && $("#hand-form")) renderForm(); break; }
    case "size": draft.table_size = +v; draft.hero.pos = POS[+v].includes(draft.hero.pos) ? draft.hero.pos : ""; draft.villains = draft.villains.filter((x) => !x.pos || POS[+v].includes(x.pos)).slice(0, +v - 1); saveDraft(); renderForm(); break;
    case "hero-pos": draft.hero.pos = draft.hero.pos === v ? "" : v; saveDraft(); renderForm(); break;
    case "stack-bb": draft.hero.stack = +v * (+draft.bb || 0); saveDraft(); renderForm(); break;
    case "pick": openPicker(b.dataset.g, +b.dataset.i); break;
    case "jump": picker.index = +v; picker.rank = null; renderSheet(); break;
    case "rank": picker.rank = v; renderSheet(); break;
    case "suit": {
      setCard(picker.targets[picker.index], picker.rank + v);
      saveDraft();
      picker.rank = null;
      const next = picker.targets.findIndex((t, k) => k > picker.index && !getCard(t));
      if (next === -1) { closeSheet(); renderForm(); } else { picker.index = next; renderSheet(); }
      break;
    }
    case "clear-card": setCard(picker.targets[picker.index], null); picker.rank = null; saveDraft(); renderSheet(); break;
    case "add-villain": draft.villains.push({ pos: "", stack: null, cards: [], note: "" }); saveDraft(); renderForm(); break;
    case "del-villain": draft.villains.splice(+b.dataset.i, 1); saveDraft(); renderForm(); break;
    case "vil-pos": { const x = draft.villains[+b.dataset.i]; x.pos = x.pos === v ? "" : v; saveDraft(); renderForm(); break; }
    case "street": ui.actStreet = v; ui.pending = null; renderForm(); break;
    case "actor": ui.pending = { pos: v, act: null }; ui.amount = ""; renderForm(); break;
    case "do": {
      const { live } = simulate(draft, ui.actStreet);
      const pos = ui.pending?.pos && live.st[ui.pending.pos] && !live.st[ui.pending.pos].folded ? ui.pending.pos : nextActor(draft, ui.actStreet, live);
      if (!pos) break;
      if (v === "bet" || v === "raise") { ui.pending = { pos, act: v }; ui.amount = ""; renderForm(); $("#amount")?.focus(); break; }
      draft.actions[ui.actStreet].push({ pos, act: v });
      ui.pending = null; saveDraft(); renderForm(); break;
    }
    case "preset": ui.amount = v; { const el = $("#amount"); if (el) el.value = v; } break;
    case "confirm-bet": confirmBet(); break;
    case "undo": draft.actions[ui.actStreet].pop(); ui.pending = null; saveDraft(); renderForm(); break;
    case "sign": ui.sign = +v; if (draft.result != null) draft.result = ui.sign * Math.abs(draft.result); saveDraft(); renderForm(); break;
    case "use-guess": draft.result = +v; ui.sign = +v < 0 ? -1 : 1; saveDraft(); renderForm(); break;
    case "tag": draft.tags = draft.tags.includes(v) ? draft.tags.filter((t) => t !== v) : [...draft.tags, v].slice(0, 12); saveDraft(); renderForm(); break;
    case "add-tag": addTag(); break;
    case "reset": if (confirm("지금 쓰던 내용을 지우고 새로 쓸까요?")) { draft = newDraft(); ui = { sign: 1, actStreet: "pre", pending: null, amount: "" }; saveDraft(); renderForm(); window.scrollTo(0, 0); } break;
    case "cancel-edit": { const id = editing; editing = null; draft = null; location.hash = "#/hand/" + id; break; }
    case "same-table": {
      const src = visibleHands().find((x) => x.id === v);
      draft = newDraft();
      Object.assign(draft, { session: src.session, table_size: src.table_size, sb: src.sb, bb: src.bb, played_at: src.played_at });
      draft.hero.stack = src.hero.stack;
      ui = { sign: 1, actStreet: "pre", pending: null, amount: "" };
      store(KEY.draft, draft); location.hash = "#/new"; break;
    }
    case "delete":
      if (!confirm("이 핸드를 삭제할까요?")) break;
      if (outbox[v] && !hands.some((x) => x.id === v)) delete outbox[v];
      else outbox[v] = { op: "delete" };
      persist(); location.hash = "#/list"; sync(); break;
    case "rp": replayIndex = +v; rerenderDetail(); break;
  }
});

window.addEventListener("hashchange", () => { replayIndex = 0; route(); window.scrollTo(0, 0); });
window.addEventListener("online", sync);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") sync(); });

route();
renderSync();
sync();
