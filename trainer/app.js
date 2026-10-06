// 레인지 트레이너 — 화면. 게임 규칙은 core.js, 차트 판정은 lib/ranges.js
const $ = (id) => document.getElementById(id);
const DB = Ranges.index(window.RANGES_DB);
DB.equity = window.EQUITY_DB || null;      // 리잼 단계 칩 EV 용 승률표
const KEY = "rangeTrainer.v1";
if (/^https?:$/.test(location.protocol) && location.pathname.startsWith("/trainer/")) {
  document.querySelectorAll(".site-link").forEach((link) => link.classList.remove("hidden"));
}

// ---------- 진행 기록 저장 ----------
function loadProgress() {
  try {
    const p = JSON.parse(localStorage.getItem(KEY));
    if (p && p.v === 1) return { ...Core.newProgress(), ...p, settings: { ...Core.newProgress().settings, ...p.settings } };
  } catch (e) { /* 저장소를 못 쓰는 환경이면 새로 시작 */ }
  return Core.newProgress();
}
function saveProgress() {
  try { localStorage.setItem(KEY, JSON.stringify(prog)); } catch (e) { /* 무시 */ }
}
let prog = loadProgress();

// ---------- 효과음 (홀덤 사이트와 같은 맑은 톤) ----------
let audioCtx = null;
function initAudio() {
  if (!audioCtx) { try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { audioCtx = null; } }
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
}
document.addEventListener("click", initAudio);
document.addEventListener("keydown", initAudio);
const SVOL = 0.7;
function pluck(freq, t0, dur, gain, type) {
  const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
  osc.type = type || "triangle";
  osc.frequency.setValueAtTime(freq, t0);
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime((gain || 0.2) * SVOL, t0 + 0.004);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(g).connect(audioCtx.destination);
  osc.start(t0); osc.stop(t0 + dur + 0.02);
}
function bell(freq, t0, dur, gain) {
  [[1, 1], [2.01, 0.45], [3.0, 0.22], [4.7, 0.1]].forEach(([r, amp]) => {
    const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
    osc.type = "sine"; osc.frequency.setValueAtTime(freq * r, t0);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime((gain || 0.2) * amp * SVOL, t0 + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g).connect(audioCtx.destination);
    osc.start(t0); osc.stop(t0 + dur + 0.02);
  });
}
function sfx(name) {
  if (!prog.settings.sound) return;
  initAudio();
  if (!audioCtx) return;
  const t = audioCtx.currentTime;
  if (name === "deal") { pluck(740, t, 0.10, 0.16); pluck(740, t + 0.09, 0.10, 0.14); }
  else if (name === "right") { bell(659, t, 0.55, 0.14); bell(988, t + 0.12, 0.6, 0.12); }
  else if (name === "half") { bell(700, t, 0.38, 0.16); }
  else if (name === "wrong") { pluck(700, t, 0.08, 0.14); pluck(440, t + 0.09, 0.18, 0.13); }
  else if (name === "raise") { bell(560, t, 0.4, 0.16); }
}

// ---------- 카드 (홀덤 cardEl 과 같은 모양) ----------
const SUIT = { s: "♠", h: "♥", d: "♦", c: "♣" };
const SUIT_ORDER = { s: 0, h: 1, d: 2, c: 3 };
const RANK_VAL = { 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, T: 10, J: 11, Q: 12, K: 13, A: 14 };
function sortHole(cards) {
  return [...cards].sort((a, b) => (SUIT_ORDER[a[1]] - SUIT_ORDER[b[1]]) || (RANK_VAL[b[0]] - RANK_VAL[a[0]]));
}
function cardEl(card) {
  const el = document.createElement("div");
  el.className = "card deal";
  const rank = card[0] === "T" ? "10" : card[0];
  const sym = SUIT[card[1]];
  if (card[1] === "h" || card[1] === "d") el.classList.add("red");
  const corner = `<span class="r">${rank}</span><span class="s">${sym}</span>`;
  el.innerHTML = `<span class="pip">${sym}</span><span class="corner-top">${corner}</span><span class="corner-bottom">${corner}</span>`;
  return el;
}
function tinyBacks() {
  const d = document.createElement("div");
  d.className = "backs";
  d.innerHTML = '<div class="card back tiny"></div><div class="card back tiny"></div>';
  return d;
}

// ---------- 게임 상태 ----------
let stageId = null, filter = "all";
let q = null;          // 지금 문제
let answered = false;
let pendingFollow = null;

const fmt = (x) => (Math.round(x * 100) / 100).toString();
function anteTotal() { return { none: 0, bb: 1, all: Core.tableOrder(q.spot).length * 0.125 }[prog.settings.ante] || 0; }
const stackText = () => q.stack == null ? "원본 스택 미표기" : `유효 ${q.stack}bb`;
const opponentText = () => q.villainLabel || Core.POS_LABEL[q.villain] || q.villain;

// ---------- 홈 ----------
function renderHome() {
  $("home").classList.remove("hidden");
  const acc = prog.answered ? Math.round((100 * prog.correct) / prog.answered) + "%" : "-";
  $("home-stats").innerHTML =
    `<span><b>${prog.score.toLocaleString()}</b>점수</span><span><b>${prog.answered}</b>푼 문제</span>` +
    `<span><b>${acc}</b>정확도</span><span><b>${prog.bestStreak}</b>최고 연속</span>` +
    `<span><b>${prog.review.length}</b>복습 대기</span>`;

  const list = $("stage-list");
  list.innerHTML = "";
  Core.STAGES.forEach((st) => {
    const open = Core.isUnlocked(prog, st);
    const s = prog.stages[st.id] || { n: 0, ok: 0 };
    const box = document.createElement("div");
    box.className = "stage" + (open ? "" : " locked");
    const progTxt = s.n ? `${s.n}문제 · 정확도 ${Math.round((100 * s.ok) / s.n)}%` : "아직 안 풀어봄";
    box.innerHTML =
      `<div class="stage-head"><span class="stage-name">${open ? "" : "🔒 "}${st.name}</span><span class="stage-prog">${progTxt}</span></div>` +
      `<div class="stage-desc">${st.desc}</div><div class="stage-row"></div>`;
    const row = box.querySelector(".stage-row");
    if (open) {
      st.filters.forEach(([fid, label]) => {
        const b = document.createElement("button");
        b.className = "chip";
        b.textContent = "▶ " + label;
        b.onclick = () => startStage(st.id, fid);
        row.appendChild(b);
      });
    } else {
      const u = st.unlock, need = Core.STAGES.find((x) => x.id === u.stage).name;
      row.innerHTML = `<span class="lock-note">${need} ${u.n}문제 이상 · 정확도 ${u.acc * 100}% 이상이면 열려요</span>`;
    }
    list.appendChild(box);
  });
  renderMastery();
}

// 자리 x 스택대 숙련도 지도
function renderMastery() {
  const cols = ["UTG", "UTG1", "UTG2", "MP", "HJ", "CO", "BTN", "SB"];
  const rows = [
    ["10-20bb", (s) => s.id.startsWith("open/openraising_10_20bb/")],
    ["20-40bb", (s) => s.id.startsWith("open/openraising_20_40bb/")],
    ["40-100bb", (s) => s.id.startsWith("open/openraising_40_100bb/")],
    ["10bb 푸시", (s) => s.id.startsWith("open/pushing_ranges_10bb/")],
  ];
  const pool = Core.stageSpots(DB, "open").concat(Core.stageSpots(DB, "push"));
  let html = "<table><caption>자리별 숙련도 (정확도 · 푼 수)</caption><tr><th></th>" +
    cols.map((c) => `<th>${Core.POS_LABEL[c]}</th>`).join("") + "</tr>";
  rows.forEach(([name, m]) => {
    html += `<tr><th>${name}</th>`;
    cols.forEach((c) => {
      const spot = pool.find((s) => m(s) && s.hero === c);
      if (!spot) { html += "<td style='background:transparent'></td>"; return; }
      const st = prog.spots[spot.id];
      if (!st || !st.n) { html += "<td>·</td>"; return; }
      const a = st.ok / st.n;
      const hue = Math.round(a * 120);   // 0 빨강 -> 120 초록
      const bg = st.n >= 3 ? `hsl(${hue},45%,30%)` : "#1a2c21";
      html += `<td style="background:${bg}" title="${spot.title}: ${st.ok}/${st.n}">${Math.round(a * 100)}%<small>${st.n}문제</small></td>`;
    });
    html += "</tr>";
  });
  html += "</table>";
  const unlocked = (id) => Core.isUnlocked(prog, Core.STAGES.find((s) => s.id === id));
  if (unlocked("vsopen")) html += pairMastery(Core.stageSpots(DB, "vsopen"), "오픈에 대응 — 내 자리 × 오프너",
    ["UTG1", "MP", "CO", "BTN", "SB", "BB"], ["UTG", "MP", "CO", "BTN", "SB"]);
  if (unlocked("rejam")) {
    html += pairMastery(Core.stageSpots(DB, "rejam", "rejam"), "리잼 — 내 자리 × 오프너",
      ["MP", "HJ", "CO", "BTN", "SB", "BB"], ["UTG", "UTG1", "UTG2", "MP", "HJ", "CO", "BTN", "SB"]);
    html += pairMastery(Core.stageSpots(DB, "rejam", "call"), "리잼콜 — 내 오픈 자리 × 리잼한 사람",
      ["UTG", "MP", "CO", "BTN", "SB"], ["UTG1", "UTG2", "MP", "HJ", "CO", "BTN", "SB", "BB"]);
  }
  for (const id of ["hu", "bvb", "bb", "special"]) {
    if (!unlocked(id)) continue;
    const spots = Core.stageSpots(DB, id);
    let n = 0, ok = 0;
    spots.forEach((s) => { const p = prog.spots[s.id]; if (p) { n += p.n; ok += p.ok; } });
    html += `<p class="hint">${Core.STAGES.find((s) => s.id === id).name}: ${spots.length}개 상황 · ${n}문제 · 정확도 ${n ? Math.round(ok / n * 100) + "%" : "-"}</p>`;
  }
  $("mastery").innerHTML = html;
}

// 두 자리 매치업 숙련도: 내 자리(행) x 상대(열), 스택대는 합산
function pairMastery(spots, caption, heroList, villainList) {
  const heroes = heroList.filter((p) => spots.some((s) => s.hero === p));
  const openers = villainList.filter((v) => spots.some((s) => s.villain.split("/").includes(v)));
  let html = `<table><caption>${caption} (정확도 · 푼 수)</caption><tr><th></th>` +
    openers.map((o) => `<th>vs ${Core.POS_LABEL[o]}</th>`).join("") + "</tr>";
  heroes.forEach((hp) => {
    html += `<tr><th>${Core.POS_LABEL[hp]}</th>`;
    openers.forEach((op) => {
      const ss = spots.filter((s) => s.hero === hp && s.villain.split("/").includes(op));
      if (!ss.length) { html += "<td style='background:transparent'></td>"; return; }
      let n = 0, ok = 0;
      ss.forEach((s) => { const st = prog.spots[s.id]; if (st) { n += st.n; ok += st.ok; } });
      if (!n) { html += "<td>·</td>"; return; }
      const a = ok / n;
      const bg = n >= 3 ? `hsl(${Math.round(a * 120)},45%,30%)` : "#1a2c21";
      html += `<td style="background:${bg}" title="${ss.length}개 스택대 합산: ${ok}/${n}">${Math.round(a * 100)}%<small>${n}문제</small></td>`;
    });
    html += "</tr>";
  });
  return html + "</table>";
}

function startStage(id, f) {
  stageId = id; filter = f;
  $("home").classList.add("hidden");
  const st = Core.STAGES.find((s) => s.id === id);
  const fl = st.filters.find(([x]) => x === f);
  $("stage-label").textContent = st.name + (f !== "all" ? " · " + fl[1] : "");
  nextQuestion();
}

// ---------- 문제 ----------
function nextQuestion() {
  if (pendingFollow) { q = pendingFollow; pendingFollow = null; }
  else q = Core.makeQuestion(DB, prog, stageId, filter);
  answered = false;
  renderQuestion();
  sfx(q.step === "vs3bet" ? "raise" : "deal");
}

function renderQuestion() {
  renderTable();
  const P = Core.POS_LABEL, hero = P[q.spot.hero];
  const hl = (s) => `<span class="hl">${s}</span>`;
  const STEP = { vsiso: "림프 후 대응 · ", vs3bet: q.situation === "vs_jam" ? "올인 대응 · " : "3벳 대응 · ", vs4bet: q.situation === "vs_jam" ? "올인 대응 · " : "4벳 대응 · " };
  $("q-title").textContent = (STEP[q.step] || "") + q.spot.title + (q.review ? " · 🔁 복습" : "");
  let html;
  if (q.step === "vs3bet" || q.step === "vsiso") {
    const first = q.after === "limp" ? "림프" : q.spot.kind === "vs_limp" ? "아이솔레이션 레이즈" : "오픈";
    const reply = q.situation === "vs_jam" ? "올인" : q.step === "vsiso" ? "레이즈" : "3벳";
    html = `${hl(hero)}에서 ${q.heroBet ?? q.openSize}bb ${first}했더니 ${hl(P[q.villain])}가 ${hl(q.threeBet + "bb")} ${reply}. ` +
      `내가 포지션 ${hl(q.pos === "ip" ? "있음" : "없음")} · 유효 ${q.stack}bb`;
  } else if (q.step === "vs4bet") {
    const back = q.situation === "vs_jam" ? `${hl("올인(" + q.stack + "bb)")}으로 받아쳤다` : `${hl(q.fourBet + "bb")}로 4벳했다`;
    html = `${hl(P[q.villain])}의 ${q.openSize}bb 오픈에 ${hl(hero)}에서 ${q.threeBet}bb로 3벳 → 상대가 ${back}. 유효 ${q.stack}bb`;
  } else if (q.spot.kind === "rejam") {
    html = `${hl(P[q.villain])}가 ${hl(q.openSize + "bb")} 오픈, 나머지 폴드. ${hl(hero)}, 유효스택 ${hl(q.stack + "bb")} — 리잼(올인)할까?`;
  } else if (q.spot.kind === "call_rejam") {
    html = `${hl(hero)}에서 ${q.openSize}bb 오픈했더니 ${hl(P[q.villain])}가 ${hl(q.stack + "bb 올인")}. 콜할까?`;
  } else if (q.spot.kind === "vs_limp") {
    html = `${q.spot.table === "hu" ? "헤즈업" : "앞에서 전부 폴드"}. ${hl("SB")}가 1bb로 림프. ${hl(hero)}, ${hl(stackText())} — 체크? 레이즈? 올인?`;
  } else if (q.spot.kind === "vs_open") {
    const size = q.facing == null ? "오픈 크기 미표기" : q.facing + "bb 오픈";
    const others = q.abstract ? "자리·스택·크기는 원본에 미표기" : q.callers.length
      ? `${q.callers.map((p) => P[p]).join("·")}가 각각 ${q.facing}bb 콜` : "나머지 폴드";
    html = `${hl(opponentText())} · ${hl(size)}, ${others}. ${hl(hero)}, ${hl(stackText())} — 어떻게 할까?`;
  } else {
    const who = q.spot.table === "hu" ? "헤즈업, SB가 첫 액션" : Core.POS_ORDER.indexOf(q.spot.hero) === 0 ? "내가 첫 액션" : "앞에서 전부 폴드";
    html = `${who}. ${hl(hero)}, ${hl(stackText())} — 어떻게 할까?`;
  }
  if (q.spot.profile) html += `<div class="context-note">전략 조건: ${q.spot.profile}</div>`;
  if (q.spot.review) html += `<div class="context-note">차트 참고: ${q.spot.review}</div>`;
  if (q.callers.length) html += '<div class="context-note">콜러 자리는 상황을 보여주기 위한 예시입니다. 정답은 해당 멀티웨이 차트 기준입니다.</div>';
  $("q-prompt").innerHTML = html;
  $("feedback").classList.add("hidden");
  renderActions();
  updateTopbar();
}

// 좌석마다 {bet, action, folded, waiting, villain} — 문제 종류에 따라 테이블 모습이 다르다
function seatState(pos) {
  const order = Core.tableOrder(q.spot);
  const hero = q.spot.hero;
  const acted = order.indexOf(pos) < order.indexOf(hero);     // 나보다 먼저 행동한 자리
  const blind = pos === "SB" ? 0.5 : pos === "BB" ? 1 : 0;
  const s = { bet: blind, action: "", folded: false, waiting: false, villain: false };
  const fold = () => Object.assign(s, { folded: true, action: "폴드" });
  const vil = q.villain || q.spot.villain;

  if (pos === hero) {
    if (q.spot.kind === "call_rejam") s.bet = q.openSize;
    if (q.step === "vs3bet" || q.step === "vsiso") s.bet = q.heroBet ?? q.openSize;
    if (q.step === "vs4bet") s.bet = q.threeBet;
    return s;
  }
  if (q.step === "vs3bet" || q.step === "vsiso" || q.step === "vs4bet") {
    if (pos !== vil) return fold();
    s.villain = true;
    if (q.step !== "vs4bet") Object.assign(s, { bet: q.threeBet, action: q.situation === "vs_jam" ? "올인" : q.step === "vsiso" ? "레이즈" : "3벳" });
    else Object.assign(s, { bet: q.fourBet, action: q.situation === "vs_jam" ? "올인" : "4벳" });
    return s;
  }
  if (q.spot.kind === "call_rejam") {
    if (pos !== vil) return fold();
    return Object.assign(s, { bet: q.stack, action: "올인", villain: true });
  }
  if ((q.spot.kind === "vs_open" || q.spot.kind === "rejam") && pos === vil)
    return Object.assign(s, { bet: q.openSize, action: "오픈", villain: true });
  if (q.spot.kind === "vs_limp" && pos === vil)
    return Object.assign(s, { bet: 1, action: "림프", villain: true });
  if (q.callers.includes(pos)) return Object.assign(s, { bet: q.openSize, action: "콜", waiting: true });
  if (acted) return fold();
  return Object.assign(s, { waiting: true, action: blind ? "블라인드" : "대기" });
}

function renderTable() {
  const seats = $("seats");
  seats.innerHTML = "";
  const order = q.abstract ? [q.spot.hero, q.villain] : Core.tableOrder(q.spot);
  const h = order.indexOf(q.spot.hero);
  const mob = window.innerWidth <= 600;
  const rx = mob ? 39 : 47, ry = mob ? 43 : 44;
  const anteBB = prog.settings.ante === "bb" ? 1 : 0;
  let pot = anteTotal();          // 앤티 + 좌석 앞에 놓인 칩 합

  for (let k = 0; k < order.length; k++) {
    const pos = order[(h + k) % order.length];
    const angle = (k / order.length) * 2 * Math.PI;
    const seat = document.createElement("div");
    seat.className = "seat";
    seat.style.left = 50 - rx * Math.sin(angle) + "%";
    seat.style.top = (k === 0 ? 86 : order.length === 2 ? 15 : 50 + ry * Math.cos(angle)) + "%";
    const st = seatState(pos);
    if (k === 0) seat.classList.add("hero");
    if (st.folded) seat.classList.add("folded");
    if (st.villain) seat.classList.add("villain");

    const plate = document.createElement("div");
    plate.className = "nameplate";
    const dealer = !q.abstract && (pos === "BTN" || q.spot.table === "hu" && pos === "SB") ? '<span class="dealer-btn">D</span>' : "";
    const left = Math.max(0, q.stack - st.bet - (pos === "BB" ? anteBB : 0));
    plate.innerHTML = `<div><span class="pos-chip">${q.abstract && k !== 0 ? opponentText() : Core.POS_LABEL[pos]}</span>${dealer}</div>` +
      `<div class="pchips">${q.stack == null ? "스택 미표기" : fmt(left) + "bb"}</div>`;

    if (k === 0) {
      const cards = document.createElement("div");
      cards.className = "hero-cards";
      sortHole(q.cards).forEach((c) => cards.appendChild(cardEl(c)));
      seat.appendChild(cards);
    } else if (st.waiting || st.villain) {
      seat.appendChild(tinyBacks());
    }
    seat.appendChild(plate);
    const act = document.createElement("div");
    act.className = "paction";
    act.textContent = st.action;
    seat.appendChild(act);
    pot += st.bet;
    if (st.bet && !q.abstract) {
      const b = document.createElement("div");
      b.className = "pbet";
      b.textContent = `🪙 ${fmt(st.bet)}`;
      seat.appendChild(b);
    }
    seats.appendChild(seat);
  }
  $("pot").textContent = q.abstract ? "레인지별 방어" : `팟 ${fmt(pot)}bb`;
  const ante = { none: "", bb: " · BB 앤티", all: " · 전원 앤티" }[prog.settings.ante];
  $("status").textContent = q.abstract ? "원본에 없는 조건은 채점에 사용하지 않아요" : `블라인드 0.5 / 1${ante}`;
}

// ---------- 행동 버튼 ----------
const KEYS = { fold: "F", check: "C", call: "C", limp: "L", raise: "R", jam: "A", other: "F" };
function actLabel(a) {
  const n = Core.actName(a, q.step, q.spot.kind);
  const heroPost = q.spot.hero === "SB" ? 0.5 : q.spot.hero === "BB" ? 1 : 0;
  let sub = "";
  if (q.step === "first") {
    const facesOpen = q.spot.kind === "vs_open" || q.spot.kind === "rejam";
    if (a === "raise" && !q.abstract) sub = facesOpen || q.spot.kind === "vs_limp" ? `약 ${fmt(Math.min(q.stack ?? Infinity, Core.raiseSize(q)))}bb` : `${q.openSize}bb`;
    if (a === "call" && !q.abstract) sub = q.spot.kind === "call_rejam" ? `${fmt(q.stack - q.openSize)}bb 더` : `${fmt(q.openSize - heroPost)}bb 더`;
    if (a === "limp") sub = `${fmt(1 - heroPost)}bb 더`;
    if (a === "other") sub = "콜이나 폴드";
  } else if (q.step === "vs3bet" || q.step === "vsiso") {
    if (a === "raise") sub = `약 ${Math.min(q.stack, Math.round(q.threeBet * 2.2))}bb`;
    if (a === "call") sub = `${fmt(q.threeBet - (q.heroBet ?? q.openSize))}bb 더`;
  } else if (q.step === "vs4bet") {
    if (a === "call") sub = `${fmt(q.fourBet - q.threeBet)}bb 더`;
  }
  if (a === "jam") sub = `${q.stack}bb`;
  return `${n}<small>${sub ? sub + " · " : ""}${KEYS[a]}</small>`;
}
function renderActions() {
  const bar = $("actions");
  bar.innerHTML = "";
  q.choices.forEach((a) => {
    const b = document.createElement("button");
    b.className = "act " + a;
    b.dataset.act = a;
    b.innerHTML = actLabel(a);
    b.onclick = () => answer(a);
    bar.appendChild(b);
  });
}

// ---------- 채점 ----------
function answer(choice) {
  if (answered) return;
  answered = true;
  const g = Core.grade(q, choice);
  const gained = Core.record(prog, q, g);
  saveProgress();
  sfx(g.verdict === "correct" ? "right" : g.verdict === "wrong" ? "wrong" : "half");

  // 레이즈(오픈·3벳)를 맞혔으면 가끔 상대가 받아친다: 오픈 -> 3벳 대응 40%, 3벳 -> 4벳/올인 대응 50%
  pendingFollow = null;
  const chance = q.spot.kind === "vs_open" ? 0.5 : 0.4;
  if (q.step === "first" && g.verdict !== "wrong" && ["raise", "limp"].includes(choice) && Math.random() < chance)
    pendingFollow = Core.makeFollowup(q, Math.random, choice);

  document.querySelectorAll("#actions .act").forEach((b) => {
    b.disabled = true;
    if (b.dataset.act === choice) b.classList.add("picked");
  });
  const next = document.createElement("button");
  next.className = "act next";
  const followTxt = !pendingFollow ? "" : pendingFollow.situation === "vs_jam" ? "상대가 올인했다 ▶"
    : pendingFollow.step === "vsiso" ? "상대가 레이즈했다 ▶" : pendingFollow.step === "vs3bet" ? "상대가 3벳했다 ▶" : "상대가 4벳했다 ▶";
  next.innerHTML = (followTxt || "다음 문제 ▶") + "<small>Space</small>";
  next.onclick = nextQuestion;
  $("actions").appendChild(next);

  renderFeedback(g, gained);
  updateTopbar(true);
}

const VERDICT = {
  correct: "정답!",
  mixed: "정답 (혼합 전략)",
  alt: "조건부 정답",
};
function renderFeedback(g, gained) {
  const v = $("verdict");
  v.className = "verdict " + g.verdict;
  let title = g.verdict === "wrong" ? (q.boundary ? "아깝다 — 경계 핸드" : "오답 — 크게 벗어남") : VERDICT[g.verdict];
  if (g.verdict === "wrong" && g.evLoss != null)
    title = g.evLoss < 0.3 ? `아깝다 — EV 차이 ${fmt(Math.max(0, g.evLoss))}bb` : `오답 — EV ${fmt(g.evLoss)}bb 손해`;
  v.innerHTML = `${title}<span class="pts">${gained > 0 ? "+" : ""}${gained}</span>`;

  const best = g.best.map((a) => Core.actName(a, q.step, q.spot.kind)).join(" / ");
  let ans = `정답: <b>${best}</b>`;
  if (g.inferred) ans += '<span class="tag">맥락 추론</span>';
  if (g.note) ans += `<div class="hint">${g.note}</div>`;
  $("answer").innerHTML = ans;

  renderEv(g);
  const ex = Core.explain(q.spot, q.hand);
  $("why").innerHTML = `<b>${q.hand}</b> 의 차트 표기 <span class="label">${ex.label}</span>` +
    "<ul>" + ex.lines.map((l) => `<li>${l}</li>`).join("") + "</ul>";

  renderMini();
  $("feedback").classList.remove("hidden");
}

// 칩 EV 비교 상자 (리잼 단계)
function renderEv(g) {
  const box = $("evbox");
  const e = q.step === "first" && q.ev;
  if (!e) { box.classList.add("hidden"); return; }
  const pct = (x) => Math.round(x * 100) + "%";
  const names = { jam: "올인", other: "리잼 안 함 (폴드로 계산)", call: "콜", fold: "폴드" };
  const acts = Object.keys(e.ev);
  const top = Math.max(...acts.map((a) => e.ev[a]));
  let html = "<div class='ev-row'><span>칩 EV (지금부터, bb)</span></div>";
  acts.forEach((a) => {
    const v = e.ev[a];
    html += `<div class="ev-row${v === top ? " best" : ""}${a === g.choice ? " mine" : ""}"><b>${names[a]}</b>` +
      `<span class="ev-val${v < 0 ? " neg" : ""}">${v > 0 ? "+" : ""}${fmt(v)}</span></div>`;
  });
  if (e.kind === "call_rejam")
    html += `<div class="ev-detail">상대 리잼 레인지(${pct(e.rangePct)}) 상대로 내 승률 <b>${pct(e.eq)}</b> · 콜에 필요한 승률 ${pct(e.needEq)}</div>`;
  else
    html += `<div class="ev-detail">상대 오픈 ${pct(e.openPct)} 중 리잼에 콜 ${pct(e.callPct)} → 폴드 확률 <b>${pct(e.fe)}</b>` +
      (e.eq != null ? ` · 콜 받으면 내 승률 <b>${pct(e.eq)}</b>` : "") + "</div>";
  if (e.approx) html += `<div class="ev-warn">※ 짝 차트가 없어 '${e.source}' 차트로 근사</div>`;
  // 차트 정답과 칩 EV 최선이 다를 때
  const evBest = acts.filter((a) => e.ev[a] === top);
  const chartBest = g.best.filter((a) => a in e.ev);
  if (chartBest.length && !chartBest.some((a) => evBest.includes(a)) && top - Math.max(...chartBest.map((a) => e.ev[a])) > 0.05)
    html += `<div class="ev-warn">칩 EV만 보면 ${names[evBest[0]]}가 ${fmt(top - Math.max(...chartBest.map((a) => e.ev[a])))}bb 더 나아요. ` +
      "차트가 다른 건 토너먼트 생존 가치(ICM)나 뒤에 남은 사람을 고려했기 때문일 수 있어요.</div>";
  box.innerHTML = html;
  box.classList.remove("hidden");
}

const MINI_COLORS = { fold: "#4a5a50", raise: "#c9952f", jam: "#d4572a", call: "#3b8a5a", limp: "#3b6ea2", check: "#3b6ea2", other: "#3d4f45" };

// 이 상황의 13x13 정답 지도 — 내 핸드 위치를 흰 테두리로
function renderMini() {
  const mini = $("mini");
  mini.innerHTML = "";
  const seen = new Set();
  const kind = q.spot.kind;
  const follow = q.step !== "first";
  Ranges.HANDS.forEach((h) => {
    const d = document.createElement("div");
    d.textContent = h;
    let rule;
    if (follow) {
      // 첫 행동으로 레이즈하는 핸드만 후속 대응을 보여준다
      const r = Ranges.resolve(q.spot, h, { stack: q.stack, size: q.facing });
      const raises = r && r.act[q.after || "raise"] > 0;
      if (!raises) {
        d.className = "none";
        d.title = `${h}: ${q.after === "limp" ? "림프 안 함" : "선행 레이즈 안 함"}`;
        seen.add("none");
        mini.appendChild(d);
        return;
      }
      rule = q.step !== "vs4bet"
        ? Ranges.followup(q.spot, h, r, q.situation || "vs_raise", { stack: q.stack, size: q.size, pos: q.pos, after: q.after || "raise" })
        : Ranges.followup(q.spot, h, r, q.situation, { stack: q.stack, size: q.facing, after: "raise" });
    } else {
      rule = Ranges.resolve(q.spot, h, { stack: q.stack, size: q.facing });
    }
    let cls = "unknown", txt = "차트에 기록 없음", mixOf = null;
    if (rule) {
      const best = Ranges.bestActions(rule);
      cls = best.length > 1 ? "mix" : best[0];
      if (best.length > 1) mixOf = best;
      txt = Core.actText(rule.act, q.step, kind) + (rule.src === "inferred" ? " (맥락 추론)" : "");
    }
    if (follow && cls === "fold") cls = "fold2";       // 레이즈 후 접는 핸드 — '애초에 안 한 핸드'와 구분
    seen.add(cls);
    d.className = cls + (h === q.hand ? " me" : "");
    if (mixOf) {   // 혼합 칸은 실제 두 행동 색으로 줄무늬
      const [c1, c2] = mixOf.map((a) => MINI_COLORS[a] || "#555");
      d.style.background = `repeating-linear-gradient(45deg, ${c1}, ${c1} 3px, ${c2} 3px, ${c2} 6px)`;
    }
    d.title = `${h}: ${txt}`;
    mini.appendChild(d);
  });
  const P = Core.POS_LABEL;
  let head;
  if (q.step === "vs3bet" || q.step === "vsiso") head = `${P[q.spot.hero]} ${q.after === "limp" ? "림프" : "레이즈"} → ${q.situation === "vs_jam" ? "올인" : "레이즈/3벳"} 받았을 때 (${q.pos === "ip" ? "포지션 있음" : "포지션 없음"}, ${q.stack}bb)`;
  else if (q.step === "vs4bet") head = `${P[q.spot.hero]} 3벳 → ${q.situation === "vs_jam" ? "올인" : "4벳"} 받았을 때 (${q.stack}bb)`;
  else if (q.spot.kind === "call_rejam") head = `${P[q.spot.hero]} 오픈 → ${P[q.villain]} 리잼 · ${q.stack}bb 일 때 콜 차트`;
  else if (q.spot.kind === "rejam") head = `${P[q.spot.hero]} vs ${P[q.villain]} 오픈 · ${q.stack}bb 일 때 리잼 차트`;
  else if (q.spot.kind === "vs_open") head = `${P[q.spot.hero]} vs ${opponentText()} · ${q.facing == null ? "크기 미표기" : q.facing + "bb 오픈"} · ${stackText()}`;
  else head = `${q.spot.title.split(" · ")[0]} · ${stackText()}`;
  $("mini-head").textContent = head;
  const raiseName = Core.actName("raise", q.step, kind);
  const names = { none: q.after === "limp" ? "림프 안 함" : "선행 레이즈 안 함", fold2: "접기", fold: "폴드", raise: raiseName, jam: "올인", call: "콜", limp: "림프", check: "체크", mix: "혼합", unknown: "기록 없음", other: "리잼 안 함" };
  const colors = { none: "#0f1a13", fold2: "#7a3434", fold: "#22382b", raise: "var(--a-raise)", jam: "var(--a-jam)", call: "var(--a-call)", limp: "var(--a-limp)", check: "var(--a-check)", mix: "var(--a-raise)", unknown: "#2a3a30", other: "#3d4f45" };
  names.mix = "혼합 (둘 다 정답 · 줄무늬 = 두 행동 색)";
  colors.mix = "repeating-linear-gradient(45deg, #c9952f, #c9952f 2px, #3b8a5a 2px, #3b8a5a 4px)";
  $("mini-legend").innerHTML = [...seen].map((c) => `<span><i style="background:${colors[c]}"></i>${names[c] || c}</span>`).join("");
}

function updateTopbar(bump) {
  $("score").textContent = prog.score.toLocaleString();
  $("streak").textContent = prog.streak;
  $("acc").textContent = prog.answered ? Math.round((100 * prog.correct) / prog.answered) + "%" : "-";
  if (bump) ["score", "streak"].forEach((id) => { const e = $(id); e.classList.remove("bump"); void e.offsetWidth; e.classList.add("bump"); });
}

// ---------- 키보드 ----------
document.addEventListener("keydown", (e) => {
  if (!$("library").classList.contains("hidden")) return;
  if (!q || !$("home").classList.contains("hidden") || !$("settings").classList.contains("hidden")) return;
  if (answered) {
    if (e.code === "Space" || e.code === "Enter") { e.preventDefault(); nextQuestion(); }
    return;
  }
  const k = e.key.toUpperCase();
  const map = { F: "fold", L: "limp", R: "raise", A: "jam", J: "jam", O: "other" };
  let a = map[k];
  if (k === "C") a = q.choices.includes("call") ? "call" : "check";
  if (k === "F" && !q.choices.includes("fold") && q.choices.includes("other")) a = "other";
  if (a && q.choices.includes(a)) { e.preventDefault(); answer(a); }
});

// ---------- 설정 / 네비게이션 ----------
$("home-btn").onclick = () => { pendingFollow = null; renderHome(); };
$("settings-open").onclick = () => {
  $("ante").value = prog.settings.ante;
  $("sound").checked = prog.settings.sound;
  $("settings").classList.remove("hidden");
};
$("settings-close").onclick = () => {
  prog.settings.ante = $("ante").value;
  prog.settings.sound = $("sound").checked;
  saveProgress();
  $("settings").classList.add("hidden");
  if (q) renderTable();
};
$("reset").onclick = () => {
  if (!confirm("점수·정확도·복습 기록을 모두 지울까요? (설정은 유지)")) return;
  const settings = prog.settings;
  prog = Core.newProgress();
  prog.settings = settings;
  saveProgress();
  $("settings").classList.add("hidden");
  renderHome();
};
window.addEventListener("resize", () => { if (q) renderTable(); });

renderHome();
updateTopbar();
