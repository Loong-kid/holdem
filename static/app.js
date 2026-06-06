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
$("settings-btn").onclick = () => { renderSettings(); openModal("settings-modal"); };
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

// Start/pause continuous dealing (host only). Handler is wired in render().
$("game-toggle-btn").onclick = () =>
  ws.send(JSON.stringify({ type: state && state.auto_running ? "pause" : "start" }));

// Personal controls: sit out / come back, and rebuy when busted.
$("sit-btn").onclick = () =>
  ws.send(JSON.stringify({ type: "sit_out", value: !(priv && priv.sitting_out) }));
$("rebuy-btn").onclick = () => ws.send(JSON.stringify({ type: "rebuy" }));

// Drag the handle at the top of a docked panel to resize it. Because the panels
// are anchored to the bottom of the screen, dragging UP makes them taller.
(function setupPanelResize() {
  let active = null, startY = 0, startH = 0;
  document.querySelectorAll(".panel-handle").forEach((h) => {
    h.addEventListener("mousedown", (e) => {
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

// ---- Main render ----------------------------------------------------------
function render() {
  if (!state) return;
  $("phase-label").textContent = state.phase;
  $("blinds-label").textContent = `블라인드 ${state.small_blind}/${state.big_blind}`;

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

  // Keep open modals in sync with fresh state.
  if (!$("settings-modal").classList.contains("hidden")) renderSettings();
  if (!$("board-modal").classList.contains("hidden")) renderBoard();

  // Community cards (pad to 5 placeholders so the table feels stable).
  const comm = $("community");
  comm.innerHTML = "";
  for (let i = 0; i < 5; i++) {
    comm.appendChild(cardEl(state.community[i] || null));
  }

  $("pot").textContent = state.pot > 0 ? `팟: ${state.pot}` : "";

  // Winner banner at showdown / uncontested win.
  if (state.results && state.results.length && !state.hand_in_progress) {
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

  ordered.forEach((p, i) => {
    // Place seat i around an ellipse; i=0 is bottom-center (me).
    const angle = (i / n) * 2 * Math.PI;
    const x = 50 + 56 * Math.sin(angle);
    const y = 50 + 50 * Math.cos(angle);

    const seat = document.createElement("div");
    seat.className = "seat";
    seat.style.left = x + "%";
    seat.style.top = y + "%";
    if (p.id === myId) seat.classList.add("me");
    if (p.id === state.to_act) seat.classList.add("active");
    if (p.folded && !p.sitting_out) seat.classList.add("folded");
    if (p.sitting_out) seat.classList.add("sitting");

    // Cards: my own (or revealed at showdown) face up, others face down.
    const cardsWrap = document.createElement("div");
    cardsWrap.className = "player-cards";
    if (p.has_cards) {
      let hole = null;
      if (p.id === myId && priv && priv.hole && priv.hole.length) hole = priv.hole;
      else if (p.hole) hole = p.hole; // revealed at showdown
      if (hole) hole.forEach((c) => cardsWrap.appendChild(cardEl(c, true)));
      else { cardsWrap.appendChild(cardEl("back", true)); cardsWrap.appendChild(cardEl("back", true)); }
    }
    seat.appendChild(cardsWrap);

    const plate = document.createElement("div");
    plate.className = "nameplate";
    // Host can click a seat to jump straight to that player's stack settings.
    if (state.host === myId) {
      plate.onclick = () => { renderSettings(); openModal("settings-modal"); };
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
  ["apply-blinds", "apply-default-stack", "apply-timeout"].forEach(
    (id) => ($(id).disabled = !isHost));
  ["sb-input", "bb-input", "default-stack-input", "timeout-input"].forEach(
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
function buildReplayFrames(record) {
  const events = record.events || [];
  const players = {};
  let order = [];
  let community = [];
  let pot = 0;
  const frames = [];
  const snap = (caption) => frames.push({
    caption, community: [...community], pot,
    players: order.map((n) => ({ ...players[n], hole: [...(players[n].hole || [])] })),
  });

  events.forEach((e) => {
    if (e.type === "start") {
      order = e.players.map((p) => p.name);
      e.players.forEach((p) => (players[p.name] = {
        name: p.name, seat: p.seat, hole: p.hole, stack: p.stack, pos: p.pos || "",
        bet: 0, folded: false, action: "", win: 0, handDesc: "",
      }));
      community = []; pot = 0;
      snap(`핸드 #${record.number} 시작 · 블라인드 ${e.sb}/${e.bb} · 버튼 ${e.button}`);
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
      community = e.cards;
      order.forEach((n) => { players[n].bet = 0; players[n].action = ""; });
      snap(`${KO_STREET[e.street] || e.street}: ${e.cards.join(" ")}`);
    } else if (e.type === "result") {
      community = e.board || community;
      let cap;
      if (e.showdown) {
        (e.reveals || []).forEach((r) => { if (players[r.name]) players[r.name].handDesc = r.hand; });
        cap = "쇼다운 — " + (e.winners || []).map((w) => `${w.name} +${w.amount}`).join(", ");
      } else {
        cap = (e.winners || []).map((w) => `${w.name} +${w.amount} (모두 폴드)`).join(", ");
      }
      (e.winners || []).forEach((w) => {
        if (players[w.name]) { players[w.name].win = w.amount; players[w.name].stack += w.amount; }
      });
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

  const board = $("replay-board");
  board.innerHTML = "";
  for (let i = 0; i < 5; i++) board.appendChild(cardEl(f.community[i] || null, false));

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
      (p.handDesc ? `<span class="rp-hand">${escapeHtml(p.handDesc)}</span>` : "") +
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
