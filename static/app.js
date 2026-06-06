// ---- Holdem web client ----------------------------------------------------
// Talks to the server over a single WebSocket. The flow is always:
//   1. user clicks something -> we send a small JSON message to the server
//   2. server runs the rules and pushes back the full game state
//   3. render() redraws the whole table from that state
// We never compute game rules here; the browser only draws what it is told.

let ws = null;
let myId = null;
let state = null;     // last public state
let priv = null;      // last private state (my hole cards + legal moves)
let actorDeadline = null;  // local wall-clock (ms) when the current actor's time runs out
let replayFrames = [];     // step-by-step snapshots of the hand being reviewed
let replayIndex = 0;
let replayList = [];       // [{number, title}] fetched on demand
let showPositions = localStorage.getItem("showPositions") !== "0";  // personal display toggle
let curTourney = null;     // last tournament state (for the top-bar clock)
let levelDeadline = null;  // local wall-clock (ms) when the current level ends, or null if paused

// Default blind ladder used when the host grows the level table (mirrors the
// server's DEFAULT_TOURNAMENT_LEVELS).
const DEFAULT_LEVELS = [
  [10, 20], [15, 30], [20, 40], [25, 50], [50, 100],
  [75, 150], [100, 200], [150, 300], [200, 400], [300, 600],
  [400, 800], [500, 1000], [700, 1400], [1000, 2000], [1500, 3000],
  [2000, 4000], [3000, 6000], [4000, 8000], [5000, 10000], [7500, 15000],
];

const $ = (id) => document.getElementById(id);

// Smooth client-side countdown. The server only tells us "seconds left" on each
// state update; we tick locally so the number counts down every quarter second.
setInterval(() => {
  const el = $("turn-timer");
  if (!el) return;
  if (actorDeadline == null) { el.textContent = ""; el.classList.remove("urgent"); return; }
  const left = Math.max(0, Math.ceil((actorDeadline - Date.now()) / 1000));
  el.textContent = "⏱ " + left + "초";
  el.classList.toggle("urgent", left <= 5);
}, 250);

// Tournament blind clock in the top bar. The server tells us the current level
// and how many seconds are left; we count down locally while it is running.
setInterval(() => {
  const el = $("tourney-label");
  if (!el) return;
  if (!curTourney || !curTourney.enabled) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  let txt = `🏆 레벨 ${curTourney.level}/${curTourney.total_levels} · 블라인드 ${curTourney.sb}/${curTourney.bb}`;
  if (curTourney.is_last) {
    txt += " · 최종 레벨";
  } else if (levelDeadline != null) {
    const left = Math.max(0, Math.round((levelDeadline - Date.now()) / 1000));
    const m = Math.floor(left / 60), s = left % 60;
    txt += ` · 다음 ${m}:${String(s).padStart(2, "0")}`;
  } else if (curTourney.time_left != null) {
    const left = Math.round(curTourney.time_left);
    const m = Math.floor(left / 60), s = left % 60;
    txt += ` · ⏸ ${m}:${String(s).padStart(2, "0")}`;
  } else {
    txt += " · 대기 중";
  }
  el.textContent = txt;
}, 250);

// ---- Join -----------------------------------------------------------------
$("join-btn").onclick = () => {
  const name = $("name-input").value.trim() || "Player";
  const room = $("room-input").value.trim() || "main";
  connect(name, room);
};

function connect(name, room) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => ws.send(JSON.stringify({ type: "join", room, name }));

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "joined") {
      myId = msg.id;
      $("room-label").textContent = "방: " + msg.room;
      $("join-screen").classList.add("hidden");
      $("game-screen").classList.remove("hidden");
    } else if (msg.type === "state") {
      state = msg.public;
      priv = msg.private;
      render();
    } else if (msg.type === "replays") {
      replayList = msg.list || [];
      renderReplayList();
    } else if (msg.type === "replay") {
      if (msg.record) {
        replayFrames = buildReplayFrames(msg.record);
        replayIndex = 0;
        renderReplayFrame();
      }
    } else if (msg.type === "error") {
      flashStatus(msg.message);
    }
  };

  ws.onclose = () => flashStatus("서버 연결이 끊어졌습니다.");
}

$("leave-btn").onclick = () => location.reload();
$("deal-btn").onclick = () => ws.send(JSON.stringify({ type: "start" }));

// ---- Modals (settings + leaderboard) --------------------------------------
function openModal(id) { $(id).classList.remove("hidden"); }
function closeModal(id) { $(id).classList.add("hidden"); }
$("settings-btn").onclick = () => { renderSettings(); initTournamentEditor(); openModal("settings-modal"); };
$("board-btn").onclick = () => { renderBoard(); openModal("board-modal"); };
$("replay-btn").onclick = () => {
  ws.send(JSON.stringify({ type: "list_replays" }));   // refresh from server/DB
  renderReplayList();
  openModal("replay-modal");
};
document.querySelectorAll(".modal-close").forEach(
  (b) => (b.onclick = () => closeModal(b.dataset.close)));
// Click outside the box closes the modal.
document.querySelectorAll(".modal").forEach((m) => {
  m.onclick = (e) => { if (e.target === m) m.classList.add("hidden"); };
});

$("apply-blinds").onclick = () =>
  ws.send(JSON.stringify({
    type: "set_blinds",
    sb: parseInt($("sb-input").value, 10),
    bb: parseInt($("bb-input").value, 10),
  }));
$("apply-default-stack").onclick = () =>
  ws.send(JSON.stringify({
    type: "set_default_stack",
    amount: parseInt($("default-stack-input").value, 10),
  }));
$("apply-variant").onclick = () =>
  ws.send(JSON.stringify({
    type: "set_variant",
    variant: $("variant-select").value,
    betting: $("betting-select").value,
  }));
$("show-positions").onchange = (e) => {
  showPositions = e.target.checked;
  localStorage.setItem("showPositions", showPositions ? "1" : "0");
  if (state) render();
};
$("apply-timeout").onclick = () =>
  ws.send(JSON.stringify({
    type: "set_timeout",
    amount: parseInt($("timeout-input").value, 10),
  }));

// ---- Tournament setup editor ----------------------------------------------
// The editor is a draft form: it loads the current server config when the
// settings modal opens, the host edits it freely (state updates won't clobber
// it), then "적용" sends the whole config to the server.
function initTournamentEditor() {
  if (!state) return;
  const t = state.tournament || {};
  $("tourney-enabled").checked = !!t.enabled;
  $("tourney-minutes").value = t.minutes || 15;
  const levels = (t.levels && t.levels.length) ? t.levels : DEFAULT_LEVELS.slice();
  $("tourney-count").value = levels.length;
  renderTourneyLevels(levels);
}

function renderTourneyLevels(levels) {
  const box = $("tourney-levels");
  box.innerHTML = "";
  const isHost = state && state.host === myId;
  levels.forEach((lv, i) => {
    const row = document.createElement("div");
    row.className = "tl-row";
    const lab = document.createElement("span");
    lab.className = "tl-lab";
    lab.textContent = "Lv " + (i + 1);
    const sb = document.createElement("input");
    sb.type = "number"; sb.min = "0"; sb.value = lv[0];
    sb.id = "tl-sb-" + i; sb.disabled = !isHost;
    const sep = document.createElement("span");
    sep.className = "tl-sep"; sep.textContent = "/";
    const bb = document.createElement("input");
    bb.type = "number"; bb.min = "1"; bb.value = lv[1];
    bb.id = "tl-bb-" + i; bb.disabled = !isHost;
    row.append(lab, sb, sep, bb);
    box.appendChild(row);
  });
}

// Read whatever rows are currently shown into a [[sb,bb], ...] array.
function readShownLevels() {
  const out = [];
  for (let i = 0; ; i++) {
    const sbEl = $("tl-sb-" + i), bbEl = $("tl-bb-" + i);
    if (!sbEl || !bbEl) break;
    out.push([parseInt(sbEl.value, 10) || 0, parseInt(bbEl.value, 10) || 1]);
  }
  return out;
}

// Changing the level count keeps existing rows and fills new ones from the
// default ladder.
$("tourney-count").onchange = () => {
  let count = Math.max(1, Math.min(20, parseInt($("tourney-count").value, 10) || 1));
  $("tourney-count").value = count;
  const cur = readShownLevels();
  const next = [];
  for (let i = 0; i < count; i++) {
    next.push(cur[i] || DEFAULT_LEVELS[Math.min(i, DEFAULT_LEVELS.length - 1)].slice());
  }
  renderTourneyLevels(next);
};

$("apply-tournament").onclick = () =>
  ws.send(JSON.stringify({
    type: "set_tournament",
    enabled: $("tourney-enabled").checked,
    minutes: parseInt($("tourney-minutes").value, 10),
    levels: readShownLevels(),
  }));

// Start/pause continuous dealing (host only). Handler is wired in render().
$("game-toggle-btn").onclick = () =>
  ws.send(JSON.stringify({ type: state && state.auto_running ? "pause" : "start" }));

// Personal controls: sit out / come back, and rebuy when busted.
$("sit-btn").onclick = () =>
  ws.send(JSON.stringify({ type: "sit_out", value: !(priv && priv.sitting_out) }));
$("rebuy-btn").onclick = () => ws.send(JSON.stringify({ type: "rebuy" }));

// On phones the docked panels become bottom-sheet drawers toggled from the top
// bar (📜 log / 💬 chat); only one is open at a time. On desktop they stay docked.
const isMobile = () => window.matchMedia("(max-width: 600px)").matches;
function toggleDrawer(which) {
  const other = which === "chat" ? "log" : "chat";
  document.body.classList.remove("show-" + other);
  document.body.classList.toggle("show-" + which);
}
$("chat-toggle").onclick = () => toggleDrawer("chat");
$("log-toggle").onclick = () => toggleDrawer("log");

// Drag the handle at the top of a docked panel to resize it. Because the panels
// are anchored to the bottom of the screen, dragging UP makes them taller.
// On mobile the handle instead closes the drawer (no drag-resize on touch).
(function setupPanelResize() {
  let active = null, startY = 0, startH = 0;
  document.querySelectorAll(".panel-handle").forEach((h) => {
    h.addEventListener("click", () => {
      if (isMobile()) document.body.classList.remove("show-" + h.dataset.target);
    });
    h.addEventListener("mousedown", (e) => {
      if (isMobile()) return;          // no drag-resize on phones
      active = $(h.dataset.target);
      startY = e.clientY;
      startH = active.offsetHeight;
      document.body.style.userSelect = "none";
      e.preventDefault();
    });
  });
  window.addEventListener("mousemove", (e) => {
    if (!active) return;
    let h = startH + (startY - e.clientY);
    h = Math.max(70, Math.min(window.innerHeight * 0.78, h));
    active.style.height = h + "px";
  });
  window.addEventListener("mouseup", () => {
    active = null;
    document.body.style.userSelect = "";
  });
})();

// Chat: send on submit (Enter), then clear the box.
$("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("chat-input").value.trim();
  if (text && ws) ws.send(JSON.stringify({ type: "chat", text }));
  $("chat-input").value = "";
});

function adjustStack(targetId, delta) {
  ws.send(JSON.stringify({ type: "adjust_stack", target: targetId, delta }));
}

// ---- Actions --------------------------------------------------------------
$("fold-btn").onclick = () => sendAction("fold");
$("check-btn").onclick = () => sendAction("check");
$("call-btn").onclick = () => sendAction("call");
$("raise-btn").onclick = () =>
  sendAction("raise", parseInt($("raise-amount").value, 10));

function sendAction(action, amount = 0) {
  ws.send(JSON.stringify({ type: "action", action, amount }));
}

$("raise-slider").oninput = () => ($("raise-amount").value = $("raise-slider").value);
$("raise-amount").oninput = () => ($("raise-slider").value = $("raise-amount").value);

// ---- Card rendering -------------------------------------------------------
const SUIT = { s: "♠", h: "♥", d: "♦", c: "♣" };
const RED = new Set(["h", "d"]);

function cardEl(card, small = false) {
  const el = document.createElement("div");
  el.className = "card" + (small ? " small" : "");
  if (!card) { el.classList.add("placeholder"); return el; }
  if (card === "back") { el.classList.add("back"); return el; }
  const rank = card[0] === "T" ? "10" : card[0];
  const suit = card[1];
  const sym = SUIT[suit];
  if (RED.has(suit)) el.classList.add("red");
  const corner = `<span class="r">${rank}</span><span class="s">${sym}</span>`;
  // Pip first so it paints behind the corner labels.
  el.innerHTML =
    `<span class="pip">${sym}</span>` +
    `<span class="corner-top">${corner}</span>` +
    `<span class="corner-bottom">${corner}</span>`;
  return el;
}

// Render one or more community boards into a container (5 cards each, padded
// with placeholders). With two boards (Double Board Omaha) each row gets a small
// numbered tag and the cards are drawn smaller so both rows fit.
function renderBoards(container, boards) {
  container.innerHTML = "";
  const list = (boards && boards.length) ? boards : [[]];
  const multi = list.length > 1;
  list.forEach((bd, bi) => {
    const row = document.createElement("div");
    row.className = "board-row" + (multi ? " multi" : "");
    if (multi) {
      const tag = document.createElement("span");
      tag.className = "board-tag";
      tag.textContent = bi + 1;
      row.appendChild(tag);
    }
    for (let i = 0; i < 5; i++) row.appendChild(cardEl(bd[i] || null, multi));
    container.appendChild(row);
  });
}

// ---- Main render ----------------------------------------------------------
function render() {
  if (!state) return;
  $("phase-label").textContent = state.phase;
  $("blinds-label").textContent = `블라인드 ${state.small_blind}/${state.big_blind}`;

  // Game-mode chip in the top bar (hidden for plain No-Limit Hold'em).
  const VLABEL = { holdem: "홀덤", omaha: "오마하", omaha2: "더블보드 오마하" };
  const vlabel = VLABEL[state.variant] || "";
  const betlabel = state.betting === "pl" ? "팟리밋" : "노리밋";
  $("mode-label").textContent =
    (state.variant === "holdem" && state.betting === "nl") ? "" : `${vlabel} · ${betlabel}`;

  // Show/hide host-only controls based on whether I'm the host.
  document.body.classList.toggle("not-host", state.host !== myId);

  // Game start/pause toggle (host only; hidden for others via CSS).
  $("game-toggle-btn").textContent = state.auto_running ? "⏸ 게임 멈춤" : "▶ 게임 시작";

  // Personal sit-out / rebuy controls reflect my current status.
  const sittingOut = priv && priv.sitting_out;
  $("sit-btn").textContent = sittingOut ? "▶ 복귀하기" : "자리비움";
  $("sit-btn").classList.toggle("accent", sittingOut);
  $("rebuy-btn").classList.toggle("hidden", !(priv && priv.can_rebuy));

  // Set the local countdown deadline from the server's "seconds left".
  if (state.hand_in_progress && state.to_act && state.time_left != null) {
    actorDeadline = Date.now() + state.time_left * 1000;
  } else {
    actorDeadline = null;
  }

  // Tournament clock: track the level deadline locally only while it is running
  // (when paused we just show the frozen remaining time, no ticking).
  curTourney = state.tournament || null;
  if (curTourney && curTourney.enabled && curTourney.running && curTourney.time_left != null) {
    levelDeadline = Date.now() + curTourney.time_left * 1000;
  } else {
    levelDeadline = null;
  }

  // Keep open modals in sync with fresh state.
  if (!$("settings-modal").classList.contains("hidden")) renderSettings();
  if (!$("board-modal").classList.contains("hidden")) renderBoard();

  // Community board(s) — one row for Hold'em/Omaha, two for Double Board Omaha.
  renderBoards($("community"), state.boards);

  $("pot").textContent = state.pot > 0 ? `팟: ${state.pot}` : "";

  // Winner banner at showdown / uncontested win.
  if (!state.hand_in_progress && state.num_boards > 1
      && state.board_winners && state.board_winners.length) {
    const parts = state.board_winners.map((bw, i) =>
      `보드${i + 1} ` + bw.map((w) =>
        `${w.name} +${w.amount}${w.hand ? " (" + w.hand + ")" : ""}`).join(", "));
    $("status").textContent = "🏆 " + parts.join("   ·   ");
  } else if (state.results && state.results.length && !state.hand_in_progress) {
    const txt = state.results
      .map((r) => `${r.name} +${r.amount}${r.hand ? " (" + r.hand + ")" : ""}`)
      .join(", ");
    $("status").textContent = "🏆 " + txt;
  } else if (!state.hand_in_progress) {
    $("status").textContent = "딜을 기다리는 중...";
  } else {
    const actor = state.players.find((p) => p.id === state.to_act);
    $("status").textContent = actor ? `${actor.name} 차례` : "";
  }

  renderSeats();
  renderControls();
  renderLog();
  renderChat();
}

function renderSeats() {
  const seats = $("seats");
  seats.innerHTML = "";

  // Rotate the player list so that *I* always sit at the bottom.
  const players = state.players;
  const n = players.length;
  let myIndex = players.findIndex((p) => p.id === myId);
  if (myIndex < 0) myIndex = 0;
  const ordered = [];
  for (let k = 0; k < n; k++) ordered.push(players[(myIndex + k) % n]);

  // Tighter ellipse on phones so the left/right seats don't hang off-screen.
  const mob = window.innerWidth <= 600;
  const rx = mob ? 47 : 56;
  const ry = mob ? 45 : 50;
  ordered.forEach((p, i) => {
    // Place seat i around an ellipse; i=0 is bottom-center (me).
    const angle = (i / n) * 2 * Math.PI;
    const x = 50 + rx * Math.sin(angle);
    const y = 50 + ry * Math.cos(angle);

    const seat = document.createElement("div");
    seat.className = "seat";
    seat.style.left = x + "%";
    seat.style.top = y + "%";
    if (p.id === myId) seat.classList.add("me");
    if (p.id === state.to_act) seat.classList.add("active");
    if (p.folded && !p.sitting_out) seat.classList.add("folded");
    if (p.sitting_out) seat.classList.add("sitting");

    // Cards: my own (or revealed at showdown) face up, others face down.
    const hc = state.hole_count || 2;
    const cardsWrap = document.createElement("div");
    cardsWrap.className = "player-cards" + (hc >= 4 ? " four" : "");
    if (p.has_cards) {
      let hole = null;
      if (p.id === myId && priv && priv.hole && priv.hole.length) hole = priv.hole;
      else if (p.hole) hole = p.hole; // revealed at showdown
      if (hole) hole.forEach((c) => cardsWrap.appendChild(cardEl(c, true)));
      else for (let k = 0; k < hc; k++) cardsWrap.appendChild(cardEl("back", true));
    }
    seat.appendChild(cardsWrap);

    const plate = document.createElement("div");
    plate.className = "nameplate";
    // Host can click a seat to jump straight to that player's stack settings.
    if (state.host === myId) {
      plate.onclick = () => { renderSettings(); initTournamentEditor(); openModal("settings-modal"); };
    }
    const isButton = p.id === state.button;
    const isHost = p.id === state.host;
    const won = (state.results || []).find((r) => r.id === p.id && !state.hand_in_progress);
    const posChip = (showPositions && p.position)
      ? `<span class="pos-chip">${p.position}</span>` : "";
    plate.innerHTML =
      `<div class="pname">${posChip}${isHost ? "👑 " : ""}${escapeHtml(p.name)}${isButton ? '<span class="dealer-btn">D</span>' : ""}</div>` +
      `<div class="pchips">${p.chips}</div>` +
      (won ? `<div class="winner-badge">WIN +${won.amount}</div>` : "");
    seat.appendChild(plate);

    const bet = document.createElement("div");
    bet.className = "pbet";
    bet.textContent = p.bet > 0 ? "🪙 " + p.bet : "";
    seat.appendChild(bet);

    const act = document.createElement("div");
    act.className = "paction";
    if (p.sitting_out) {
      act.innerHTML = '<span class="sitting-badge">자리비움</span>';
    } else if (p.chips === 0 && !p.in_hand) {
      act.innerHTML = '<span class="sitting-badge">잔액 없음</span>';
    } else {
      act.textContent = p.all_in ? "ALL-IN" : (p.folded ? "fold" : "");
    }
    seat.appendChild(act);

    seats.appendChild(seat);
  });
}

function renderControls() {
  const actionBar = $("action-bar");
  const waitingBar = $("waiting-bar");

  // Between hands, show a status hint. Start/pause now lives in the top bar.
  waitingBar.classList.toggle("hidden", state.hand_in_progress);
  $("deal-btn").style.display = "none";
  const isHost = state.host === myId;
  const hint = document.querySelector(".waiting-hint");
  if (state.auto_running) {
    hint.textContent = "다음 핸드를 준비하는 중...";
  } else if (isHost) {
    hint.textContent = "상단의 ▶ 게임 시작을 눌러 진행하세요. (2명 이상 필요)";
  } else {
    hint.textContent = "방장이 게임을 시작하기를 기다리는 중...";
  }

  const myTurn = priv && priv.your_turn && priv.legal;
  actionBar.classList.toggle("hidden", !myTurn);
  if (!myTurn) return;

  const L = priv.legal;
  $("fold-btn").disabled = !L.can_fold;
  $("check-btn").style.display = L.can_check ? "" : "none";
  $("call-btn").style.display = L.can_call ? "" : "none";
  $("call-btn").textContent = `콜 ${L.call_amount}`;

  const rg = document.querySelector(".raise-group");
  if (L.can_raise) {
    rg.style.display = "flex";
    const s = $("raise-slider");
    s.min = L.min_raise_to;
    s.max = L.max_raise_to;
    s.value = L.min_raise_to;
    $("raise-amount").min = L.min_raise_to;
    $("raise-amount").max = L.max_raise_to;
    $("raise-amount").value = L.min_raise_to;
    $("raise-btn").textContent =
      L.max_raise_to === L.min_raise_to ? "올인" : "레이즈";
  } else {
    rg.style.display = "none";
  }
}

function renderSettings() {
  if (!state) return;
  $("db-status").innerHTML = state.db
    ? '데이터 저장: <span class="ok">PostgreSQL 연결됨 ✅</span> (리플레이가 영구 저장됩니다)'
      + `<div class="ver">버전 ${state.version || "?"}</div>`
    : '데이터 저장: <span class="warn">메모리(임시) ⚠️</span> (서버 재시작 시 리플레이가 사라집니다)'
      + `<div class="ver">버전 ${state.version || "?"}</div>`;
  $("show-positions").checked = showPositions;   // personal, not host-gated
  const isHost = state.host === myId;
  $("settings-note").textContent = isHost
    ? "방장으로서 아래 설정을 변경할 수 있습니다."
    : "방장만 설정을 변경할 수 있습니다 (보기 전용).";

  // Fill inputs, but never clobber a field the host is actively typing in.
  const set = (id, val) => { if (document.activeElement.id !== id) $(id).value = val; };
  set("sb-input", state.small_blind);
  set("bb-input", state.big_blind);
  set("default-stack-input", state.starting_chips);
  set("timeout-input", state.action_timeout);
  set("variant-select", state.variant || "holdem");
  set("betting-select", state.betting || "nl");
  ["variant-select", "betting-select", "apply-variant"].forEach(
    (id) => ($(id).disabled = !isHost));
  // In tournament mode the blind schedule owns the blinds, so lock the manual
  // SB/BB fields and show a note explaining why.
  const tourneyOn = !!(state.tournament && state.tournament.enabled);
  $("blinds-tourney-note").classList.toggle("hidden", !tourneyOn);
  ["apply-default-stack", "apply-timeout"].forEach(
    (id) => ($(id).disabled = !isHost));
  ["default-stack-input", "timeout-input"].forEach(
    (id) => ($(id).disabled = !isHost));
  ["apply-blinds", "sb-input", "bb-input"].forEach(
    (id) => ($(id).disabled = !isHost || tourneyOn));
  ["tourney-enabled", "tourney-minutes", "tourney-count", "apply-tournament"].forEach(
    (id) => ($(id).disabled = !isHost));

  // Per-player stack adjust rows.
  const list = $("adjust-list");
  list.innerHTML = "";
  if (!isHost) {
    list.innerHTML = '<p class="muted">방장만 스택을 조절할 수 있습니다.</p>';
    return;
  }
  if (state.hand_in_progress) {
    list.innerHTML = '<p class="muted">핸드가 끝난 뒤(딜 사이)에 조절할 수 있습니다.</p>';
  }
  state.players.forEach((p) => {
    const row = document.createElement("div");
    row.className = "adjust-row";
    const amt = document.createElement("input");
    amt.type = "number"; amt.min = "1"; amt.value = "100";
    amt.disabled = state.hand_in_progress;
    const add = document.createElement("button");
    add.className = "arow-add"; add.textContent = "+추가";
    add.disabled = state.hand_in_progress;
    add.onclick = () => adjustStack(p.id, Math.abs(parseInt(amt.value, 10) || 0));
    const rem = document.createElement("button");
    rem.className = "arow-remove"; rem.textContent = "−빼기";
    rem.disabled = state.hand_in_progress;
    rem.onclick = () => adjustStack(p.id, -Math.abs(parseInt(amt.value, 10) || 0));
    const nm = document.createElement("span");
    nm.className = "arow-name"; nm.textContent = p.name;
    const ch = document.createElement("span");
    ch.className = "arow-chips"; ch.textContent = p.chips;
    row.append(nm, ch, amt, add, rem);
    list.appendChild(row);
  });
}

function renderBoard() {
  if (!state) return;
  const body = $("board-body");
  body.innerHTML = "";
  (state.ledger || []).forEach((r) => {
    const tr = document.createElement("tr");
    if (!r.active) tr.className = "row-inactive";
    const netClass = r.net > 0 ? "net-pos" : r.net < 0 ? "net-neg" : "";
    const sign = r.net > 0 ? "+" : "";
    tr.innerHTML =
      `<td>${escapeHtml(r.name)}${r.active ? "" : " (떠남)"}</td>` +
      `<td>${r.buyin}</td><td>${r.added}</td><td>${r.removed}</td>` +
      `<td>${r.stack}</td>` +
      `<td class="${netClass}">${sign}${r.net}</td>`;
    body.appendChild(tr);
  });
  if (!body.children.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">아직 기록이 없습니다.</td></tr>';
  }
}

// ---- Replay viewer --------------------------------------------------------
$("replay-prev").onclick = () => { replayIndex--; renderReplayFrame(); };
$("replay-next").onclick = () => { replayIndex++; renderReplayFrame(); };

function renderReplayList() {
  const box = $("replay-list");
  box.innerHTML = "";
  const items = replayList;
  if (!items.length) {
    box.innerHTML = '<p class="muted">아직 기록된 핸드가 없습니다.</p>';
    return;
  }
  items.forEach((r) => {
    const d = document.createElement("div");
    d.className = "replay-item";
    d.textContent = r.title;
    d.onclick = () => {
      [...box.children].forEach((c) => c.classList.remove("sel"));
      d.classList.add("sel");
      ws.send(JSON.stringify({ type: "get_replay", number: r.number }));
    };
    box.appendChild(d);
  });
}

function koAction(label) {
  if (!label) return "";
  if (label.startsWith("fold")) return "폴드";
  if (label.startsWith("check")) return "체크";
  if (label.startsWith("call")) return "콜";
  if (label.startsWith("all-in")) return "올인" + label.slice(6);
  if (label.startsWith("raise")) return "레이즈" + label.slice(5);
  return label;
}
const KO_STREET = { flop: "플롭", turn: "턴", river: "리버" };

// Replay events -> a list of full snapshots the user can step through.
// Handles both the new multi-board format (e.boards / e.board_winners / r.hands)
// and the older single-board format (e.cards / e.board / e.winners / r.hand).
function buildReplayFrames(record) {
  const events = record.events || [];
  const VLABEL = { holdem: "홀덤", omaha: "오마하", omaha2: "더블보드 오마하" };
  const players = {};
  let order = [];
  let boards = [[]];
  let pot = 0;
  const frames = [];
  const snap = (caption) => frames.push({
    caption, boards: boards.map((b) => [...b]), pot,
    players: order.map((n) => ({ ...players[n], hole: [...(players[n].hole || [])] })),
  });

  events.forEach((e) => {
    if (e.type === "start") {
      order = e.players.map((p) => p.name);
      e.players.forEach((p) => (players[p.name] = {
        name: p.name, seat: p.seat, hole: p.hole, stack: p.stack, pos: p.pos || "",
        bet: 0, folded: false, action: "", win: 0, hands: [],
      }));
      boards = [[]]; pot = 0;
      const v = VLABEL[e.variant] ? ` · ${VLABEL[e.variant]}` : "";
      snap(`핸드 #${record.number} 시작${v} · 블라인드 ${e.sb}/${e.bb}`);
    } else if (e.type === "post") {
      const p = players[e.name];
      if (p) { p.stack -= e.amount; p.bet += e.amount; p.action = e.blind; }
      pot = e.pot;
      snap(`${e.name} · ${e.blind} ${e.amount}`);
    } else if (e.type === "action") {
      const p = players[e.name];
      if (p) {
        p.stack -= e.paid; p.bet += e.paid; p.action = koAction(e.label);
        if (e.label && e.label.startsWith("fold")) p.folded = true;
      }
      pot = e.pot;
      snap(`${e.name} — ${koAction(e.label)}`);
    } else if (e.type === "street") {
      boards = e.boards || (e.cards ? [e.cards] : boards);
      order.forEach((n) => { players[n].bet = 0; players[n].action = ""; });
      const shown = boards.map((b) => b.join(" ")).join("   |   ");
      snap(`${KO_STREET[e.street] || e.street}: ${shown}`);
    } else if (e.type === "result") {
      boards = e.boards || (e.board ? [e.board] : boards);
      let cap;
      if (e.showdown) {
        (e.reveals || []).forEach((r) => {
          if (players[r.name]) players[r.name].hands = r.hands || (r.hand ? [r.hand] : []);
        });
        if (e.board_winners) {
          cap = "쇼다운 — " + e.board_winners.map((bw, i) =>
            `보드${i + 1} ` + bw.map((w) => `${w.name} +${w.amount}`).join(", ")).join("   ·   ");
          e.board_winners.forEach((bw) => bw.forEach((w) => {
            if (players[w.name]) { players[w.name].win += w.amount; players[w.name].stack += w.amount; }
          }));
        } else {
          cap = "쇼다운 — " + (e.winners || []).map((w) => `${w.name} +${w.amount}`).join(", ");
          (e.winners || []).forEach((w) => {
            if (players[w.name]) { players[w.name].win = w.amount; players[w.name].stack += w.amount; }
          });
        }
      } else {
        cap = (e.winners || []).map((w) => `${w.name} +${w.amount} (모두 폴드)`).join(", ");
        (e.winners || []).forEach((w) => {
          if (players[w.name]) { players[w.name].win = w.amount; players[w.name].stack += w.amount; }
        });
      }
      pot = 0;
      order.forEach((n) => (players[n].bet = 0));
      snap(cap);
    }
  });
  return frames;
}

function renderReplayFrame() {
  if (!replayFrames.length) {
    $("replay-board").innerHTML = "";
    $("replay-players").innerHTML = '<p class="muted">왼쪽에서 핸드를 선택하세요.</p>';
    $("replay-pot").textContent = "";
    $("replay-caption").textContent = "";
    $("replay-step").textContent = "";
    return;
  }
  replayIndex = Math.max(0, Math.min(replayFrames.length - 1, replayIndex));
  const f = replayFrames[replayIndex];

  renderBoards($("replay-board"), f.boards);

  $("replay-pot").textContent = `팟: ${f.pot}`;

  const pl = $("replay-players");
  pl.innerHTML = "";
  f.players.forEach((p) => {
    const row = document.createElement("div");
    row.className = "rp-row" + (p.folded ? " folded" : "");
    const cards = document.createElement("div");
    cards.className = "rp-cards";
    (p.hole || []).forEach((c) => cards.appendChild(cardEl(c, true)));
    const info = document.createElement("div");
    info.className = "rp-info";
    info.innerHTML =
      (p.pos ? `<span class="rp-pos">${escapeHtml(p.pos)}</span>` : "") +
      `<span class="rp-name">${escapeHtml(p.name)}</span>` +
      `<span class="rp-stack">${p.stack}</span>` +
      (p.bet > 0 ? `<span class="rp-bet">🪙${p.bet}</span>` : "") +
      (p.action ? `<span class="rp-act">${escapeHtml(p.action)}</span>` : "") +
      (p.hands && p.hands.length ? `<span class="rp-hand">${p.hands.map(escapeHtml).join(" / ")}</span>` : "") +
      (p.win > 0 ? `<span class="rp-win">+${p.win}</span>` : "");
    row.appendChild(cards);
    row.appendChild(info);
    pl.appendChild(row);
  });

  $("replay-caption").textContent = f.caption;
  $("replay-step").textContent = `${replayIndex + 1} / ${replayFrames.length}`;
}

function renderChat() {
  const box = $("chat-messages");
  // Don't fight the user if they've scrolled up to read history.
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  box.innerHTML = "";
  (state.chat || []).forEach((m) => {
    const d = document.createElement("div");
    d.innerHTML = `<span class="cname">${escapeHtml(m.name)}:</span> ${escapeHtml(m.text)}`;
    box.appendChild(d);
  });
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderLog() {
  const log = $("log-body");
  log.innerHTML = "";
  (state.log || []).forEach((line) => {
    const d = document.createElement("div");
    d.textContent = line;
    log.appendChild(d);
  });
  log.scrollTop = log.scrollHeight;
}

// ---- Small helpers --------------------------------------------------------
function flashStatus(text) {
  $("status").textContent = text;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
