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
let timeoutTotal = 30;     // server's per-action time limit, for the countdown gauge
let pausedTimeLeft = null;  // frozen seconds left to show while the table is paused

// Default blind ladder used when the host grows the level table (mirrors the
// server's DEFAULT_TOURNAMENT_LEVELS).
const DEFAULT_LEVELS = [
  [10, 20], [15, 30], [20, 40], [25, 50], [50, 100],
  [75, 150], [100, 200], [150, 300], [200, 400], [300, 600],
  [400, 800], [500, 1000], [700, 1400], [1000, 2000], [1500, 3000],
  [2000, 4000], [3000, 6000], [4000, 8000], [5000, 10000], [7500, 15000],
];

const $ = (id) => document.getElementById(id);

// ---- Sound (Web Audio synthesis, no files) --------------------------------
// Browsers block audio until the user interacts with the page, so we lazily
// create the AudioContext and resume it on the first click. All sounds are
// generated from oscillators + short noise bursts — zero asset files.
let soundOn = localStorage.getItem("soundOn") !== "0";
let audioCtx = null;

function initAudio() {
  if (!audioCtx) {
    try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    catch (e) { audioCtx = null; }
  }
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
}
document.addEventListener("click", initAudio);   // unlock on any user gesture

const SVOL = 0.7;   // master volume (matches the preview's default level)

// Plucked note: quick (click-free) attack, fast exponential decay = clean blip.
function pluck(freq, t0, dur, gain, type) {
  const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
  osc.type = type || "triangle";
  osc.frequency.setValueAtTime(freq, t0);
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime((gain || 0.2) * SVOL, t0 + 0.004);  // ~4ms attack, no click
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(g).connect(audioCtx.destination);
  osc.start(t0); osc.stop(t0 + dur + 0.02);
}

// Bell/ding: fundamental + inharmonic partials, each decaying = clear bell tone.
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

// Crisp tonal knock: low body + a quick high harmonic for clarity.
function knock(freq, t0, gain) {
  pluck(freq, t0, 0.07, gain || 0.22, "triangle");
  pluck(freq * 3.2, t0, 0.03, (gain || 0.22) * 0.35, "sine");
}

function sfx(name) {
  if (!soundOn) return;
  initAudio();
  if (!audioCtx) return;
  const t = audioCtx.currentTime;
  if (name === "deal") {                 // card dealt / board street
    pluck(740, t, 0.10, 0.16, "triangle");
  } else if (name === "chip") {          // bet / call / raise / all-in
    bell(700, t, 0.38, 0.18);
  } else if (name === "check") {         // table knock
    knock(210, t, 0.22); knock(210, t + 0.14, 0.20);
  } else if (name === "fold") {          // clean descend
    pluck(700, t, 0.08, 0.14); pluck(440, t + 0.09, 0.15, 0.13);
  } else if (name === "turn") {          // your-turn bell
    bell(820, t, 0.5, 0.16);
  } else if (name === "win") {           // soft chime
    bell(659, t, 0.55, 0.14); bell(988, t + 0.14, 0.6, 0.12);
  }
}

// Detect what changed between the previous state and the new one, and play the
// matching sound. (The server only sends state, so we infer events by diffing.)
let sndPrev = null;
function playSounds() {
  if (!state) return;
  const cards = (state.boards || []).reduce((n, b) => n + b.filter(Boolean).length, 0);
  const myTurn = !!(priv && priv.your_turn);
  const acts = {};
  (state.players || []).forEach((p) => (acts[p.id] = p.last_action || ""));
  if (sndPrev === null) {           // first frame: snapshot only, don't blast sounds
    sndPrev = { hand: state.hand_in_progress, cards, myTurn, acts };
    return;
  }
  // Dealing: a new hand started, or a new street's board cards appeared.
  if (state.hand_in_progress && (!sndPrev.hand || cards > sndPrev.cards)) sfx("deal");
  // Per-player action sounds (their last_action changed to something new).
  (state.players || []).forEach((p) => {
    const a = p.last_action || "";
    if (a && a !== (sndPrev.acts[p.id] || "")) {
      if (a.startsWith("fold")) sfx("fold");
      else if (a.startsWith("check")) sfx("check");
      else sfx("chip");             // call / bet / raise / all-in
    }
  });
  if (myTurn && !sndPrev.myTurn) sfx("turn");       // it's my turn now
  if (!state.hand_in_progress && sndPrev.hand
      && state.results && state.results.length) sfx("win");
  sndPrev = { hand: state.hand_in_progress, cards, myTurn, acts };
}

// Smooth client-side countdown. The server only tells us "seconds left" on each
// state update; we tick locally so the number counts down every quarter second.
setInterval(() => {
  const el = $("turn-timer");
  const bar = $("turn-bar");
  const fill = $("turn-bar-fill");
  if (!el) return;
  const paused = actorDeadline == null && pausedTimeLeft != null;
  if (actorDeadline == null && !paused) {
    el.textContent = ""; el.classList.remove("urgent");
    if (bar) bar.classList.add("hidden");
    return;
  }
  const remain = paused ? pausedTimeLeft : Math.max(0, (actorDeadline - Date.now()) / 1000);
  const left = Math.ceil(remain);
  el.textContent = paused ? ("⏸ " + left + "초 (일시정지)") : ("⏱ " + left + "초");
  const urgent = !paused && left <= 5;
  el.classList.toggle("urgent", urgent);
  if (bar && fill) {
    bar.classList.remove("hidden");
    const pct = Math.max(0, Math.min(100, (remain / (timeoutTotal || 30)) * 100));
    fill.style.width = pct + "%";
    fill.classList.toggle("urgent", urgent);
  }
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

// ---- Join (spectate) + sit/stand + auto-reconnect -------------------------
// Joining a room makes you a SPECTATOR; you only get a seat after pressing
// "테이블에 앉기" (sit). A per-browser token lets the same browser auto-resume
// its seat (and stops one browser from grabbing multiple seats).
let myName = null, myRoom = null;   // remembered so we can rejoin the same seat
let reconnectTries = 0;
let lastSpectatorNames = [];        // spectator display names (for the click popup)
let awaitingRejoin = false;         // this socket is a reconnect attempt awaiting "joined"
let joinedOnce = false;             // we've successfully entered a room at least once

function getToken() {
  let t = localStorage.getItem("playerToken");
  if (!t) {
    t = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : Date.now().toString(36) + Math.random().toString(36).slice(2);
    localStorage.setItem("playerToken", t);
  }
  return t;
}

$("join-btn").onclick = () => {
  myName = $("name-input").value.trim() || "Player";
  myRoom = $("room-input").value.trim() || "main";
  reconnectTries = 0;
  openSocket(false);
};

function openSocket(isReconnect) {
  awaitingRejoin = !!isReconnect;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () =>
    ws.send(JSON.stringify({ type: "join", room: myRoom, name: myName, token: getToken() }));
  ws.onmessage = (ev) => onSocketMessage(JSON.parse(ev.data));
  ws.onclose = () => onSocketClose();
}

function enterGame(room, name) {
  myRoom = room || myRoom;
  if (name) myName = name;
  joinedOnce = true;
  localStorage.setItem("lastRoom", myRoom);
  if (myName) localStorage.setItem("lastName", myName);
  $("room-label").textContent = "방: " + myRoom;
  $("join-screen").classList.add("hidden");
  $("game-screen").classList.remove("hidden");
}

function onSocketMessage(msg) {
  if (msg.type === "joined") {
    myId = msg.seated ? msg.id : null;     // null => spectator
    reconnectTries = 0;
    awaitingRejoin = false;
    enterGame(msg.room, msg.name);
    flashStatus("");
  } else if (msg.type === "seated") {
    myId = msg.id;
    if (msg.name) { myName = msg.name; localStorage.setItem("lastName", msg.name); }
    flashStatus("");
  } else if (msg.type === "stood") {
    myId = null;
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
    if (awaitingRejoin) {
      // Rejoin refused (server may not have noticed our old socket dropped yet);
      // close and let the reconnect loop retry shortly.
      try { ws.close(); } catch (e) {}
    } else {
      flashStatus(msg.message);
    }
  }
}

function onSocketClose() {
  if (joinedOnce && reconnectTries < 90) {
    // Keep trying to resume (~90 * 2s = 180s, covers the server grace window).
    reconnectTries++;
    flashStatus(`연결이 끊겼습니다. 재접속 시도 중... (${reconnectTries})`);
    setTimeout(() => openSocket(true), 2000);
  } else if (joinedOnce) {
    flashStatus("재접속 실패. 새로고침(F5) 후 다시 들어오세요.");
  } else {
    flashStatus("서버 연결이 끊어졌습니다.");
  }
}

// Mobile: when the tab returns to the foreground the socket/timers may have been
// frozen in the background. Reconnect immediately so the seat resumes (via token)
// instead of waiting for the throttled retry loop to fire.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (!joinedOnce || reconnectTries >= 99) return;      // skip after intentional leave
  if (!ws || ws.readyState === WebSocket.CLOSING || ws.readyState === WebSocket.CLOSED) {
    reconnectTries = 0;
    openSocket(true);
  }
});

// Take a seat / leave a seat.
$("take-seat-btn").onclick = () => {
  const nm = (myName && myName.trim()) || $("name-input").value.trim() || "Player";
  myName = nm;
  ws.send(JSON.stringify({ type: "sit", name: nm, token: getToken() }));
};
$("stand-btn").onclick = () => {
  if (confirm("자리에서 일어나 관전 모드로 전환할까요? (스택 기록은 남습니다)"))
    ws.send(JSON.stringify({ type: "stand" }));
};

// Click the spectator label to see WHO is watching.
$("spectator-label").onclick = () => {
  const open = document.getElementById("spec-popup");
  if (open) { open.remove(); return; }
  if (!lastSpectatorNames.length) return;
  const pop = document.createElement("div");
  pop.id = "spec-popup";
  pop.style.cssText = "position:fixed;z-index:200;background:#16271d;border:1px solid #3c5c48;" +
    "border-radius:8px;padding:8px 12px;font-size:13px;color:#cfe3d6;max-height:50vh;" +
    "overflow:auto;box-shadow:0 4px 16px rgba(0,0,0,.5);min-width:120px";
  const t = document.createElement("b");
  t.textContent = `관전자 ${lastSpectatorNames.length}명`;
  t.style.cssText = "display:block;color:#e8c468;margin-bottom:4px";
  pop.appendChild(t);
  lastSpectatorNames.forEach(n => {
    const d = document.createElement("div"); d.textContent = n; pop.appendChild(d);
  });
  document.body.appendChild(pop);
  const r = $("spectator-label").getBoundingClientRect();
  pop.style.left = Math.max(6, r.left) + "px";
  pop.style.top = (r.bottom + 4) + "px";
  setTimeout(() => document.addEventListener("click", function close(e) {
    if (!pop.contains(e.target) && e.target !== $("spectator-label")) {
      pop.remove(); document.removeEventListener("click", close);
    }
  }), 0);
};

$("leave-btn").onclick = () => {
  reconnectTries = 99;                 // intentional leave -> do not auto-reconnect
  joinedOnce = false;
  localStorage.removeItem("lastRoom");
  localStorage.removeItem("lastName");
  try { ws.send(JSON.stringify({ type: "leave" })); } catch (e) {}
  setTimeout(() => location.reload(), 120);
};
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
$("allow-ip").onchange = (e) =>
  ws.send(JSON.stringify({ type: "set_allow_same_ip", value: e.target.checked }));
$("show-positions").onchange = (e) => {
  showPositions = e.target.checked;
  localStorage.setItem("showPositions", showPositions ? "1" : "0");
  if (state) render();
};
$("sound-on").onchange = (e) => {
  soundOn = e.target.checked;
  localStorage.setItem("soundOn", soundOn ? "1" : "0");
  if (soundOn) { initAudio(); sfx("chip"); }   // unlock + play a test blip
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

// ---- Pre-actions ("선행 액션") --------------------------------------------
// Queue a move on an opponent's turn; it fires automatically (once) when it
// becomes your turn. The three options are robust to bets changing in between:
//   fold       -> always fold
//   checkfold  -> check if legal, otherwise fold
//   callany    -> call whatever the price is (or check if nothing to call)
let preAction = null;
document.querySelectorAll(".pre-btn").forEach((b) => {
  b.onclick = () => {
    preAction = (preAction === b.dataset.pre) ? null : b.dataset.pre;  // toggle
    updatePreactionUI();
  };
});
function updatePreactionUI() {
  document.querySelectorAll(".pre-btn").forEach(
    (b) => b.classList.toggle("sel", preAction === b.dataset.pre));
}
function executePreAction(a) {
  const L = priv && priv.legal;
  if (!L) return;
  if (a === "fold") sendAction("fold");
  else if (a === "checkfold") sendAction(L.can_check ? "check" : "fold");
  else if (a === "callany") {
    if (L.can_call) sendAction("call");
    else if (L.can_check) sendAction("check");
  }
}

$("raise-slider").oninput = () => ($("raise-amount").value = $("raise-slider").value);
$("raise-amount").oninput = () => ($("raise-slider").value = $("raise-amount").value);

// Bet-sizing shortcuts: fill the raise amount with a fraction of the pot (or
// all-in). A "pot" raise = call + the pot after calling, matching pot-limit.
document.querySelectorAll(".bet-preset").forEach((b) => {
  b.onclick = () => {
    if (!priv || !priv.legal || !priv.legal.can_raise) return;
    const L = priv.legal;
    let target;
    if (b.dataset.frac === "max") {
      target = L.max_raise_to;
    } else {
      const frac = parseFloat(b.dataset.frac);
      const pot = (state && state.pot) || 0;
      target = (state.current_bet || 0) + Math.round(frac * (pot + L.to_call));
    }
    target = Math.max(L.min_raise_to, Math.min(L.max_raise_to, target));
    $("raise-slider").value = target;
    $("raise-amount").value = target;
  };
});

// ---- Card rendering -------------------------------------------------------
const SUIT = { s: "♠", h: "♥", d: "♦", c: "♣" };
const RED = new Set(["h", "d"]);

// Hole-card display order: by suit ♠ ♥ ♦ ♣ left-to-right, and within a suit the
// higher rank to the left. (Display only — never affects game logic.)
const SUIT_ORDER = { s: 0, h: 1, d: 2, c: 3 };
const RANK_VAL = { 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, T: 10, J: 11, Q: 12, K: 13, A: 14 };
function sortHole(cards) {
  return [...(cards || [])].sort((a, b) => {
    const sa = SUIT_ORDER[a[1]] ?? 9, sb = SUIT_ORDER[b[1]] ?? 9;
    if (sa !== sb) return sa - sb;
    return (RANK_VAL[b[0]] || 0) - (RANK_VAL[a[0]] || 0);
  });
}

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

  // Show/hide host-only controls based on whether I'm the host. A spectator
  // (no myId) is never host — even before anyone has taken a seat (host null).
  document.body.classList.toggle("not-host", !myId || state.host !== myId);
  // Spectator mode = I'm connected but not seated (myId is null).
  document.body.classList.toggle("spectating", !myId);
  const specs = state.spectators || 0;
  lastSpectatorNames = state.spectator_names || [];
  let specText = "";
  if (!myId) specText = "👀 관전 중" + (specs > 1 ? ` · 관전 ${specs}명` : "");
  else if (specs > 0) specText = `👀 관전 ${specs}명`;
  if (specText && specs > 0) specText += " ▾";   // 클릭하면 명단
  $("spectator-label").textContent = specText;
  $("spectator-label").classList.toggle("hidden", !specText);
  $("spectator-label").style.cursor = specs > 0 ? "pointer" : "default";

  // Game start/pause toggle (host only; hidden for others via CSS).
  $("game-toggle-btn").textContent = state.auto_running ? "⏸ 게임 멈춤" : "▶ 게임 시작";

  // Personal sit-out / rebuy controls reflect my current status.
  const sittingOut = priv && priv.sitting_out;
  $("sit-btn").textContent = sittingOut ? "▶ 복귀하기" : "자리비움";
  $("sit-btn").classList.toggle("accent", sittingOut);
  $("rebuy-btn").classList.toggle("hidden", !(priv && priv.can_rebuy));

  // Set the local countdown deadline from the server's "seconds left". While the
  // table is paused we freeze it (show the number, don't tick down).
  if (state.action_timeout) timeoutTotal = state.action_timeout;
  if (state.hand_in_progress && state.to_act && state.time_left != null) {
    if (state.auto_running) {
      actorDeadline = Date.now() + state.time_left * 1000;
      pausedTimeLeft = null;
    } else {
      actorDeadline = null;
      pausedTimeLeft = state.time_left;
    }
  } else {
    actorDeadline = null;
    pausedTimeLeft = null;
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
  } else if (!state.auto_running) {
    $("status").textContent = "⏸ 일시정지 — 방장이 재개하면 이어서 진행됩니다.";
  } else if (state.runout) {
    $("status").textContent = "🃏 올인! 보드를 공개하는 중...";
  } else {
    const actor = state.players.find((p) => p.id === state.to_act);
    $("status").textContent = actor ? `${actor.name} 차례` : "";
  }

  renderSeats();
  renderControls();
  renderHandRank();
  renderLog();
  renderChat();
  playSounds();
}

// Live "what do I have" readout (e.g. "J-7 Full House"), per board in Omaha.
function renderHandRank() {
  const hr = $("hand-rank");
  const hands = (priv && priv.hands) ? priv.hands : [];
  if (!hands.length || !state.hand_in_progress) { hr.classList.add("hidden"); return; }
  hr.classList.remove("hidden");
  if (hands.length > 1) {
    hr.innerHTML = "내 패 &nbsp; " + hands.map((h, i) =>
      `<span class="hr-tag">${i + 1}</span><b>${escapeHtml(h)}</b>`).join("&nbsp;&nbsp;&nbsp;");
  } else {
    hr.innerHTML = `내 패 &nbsp; <b>${escapeHtml(hands[0])}</b>`;
  }
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
    // Place seat i around an ellipse; i=0 is bottom-center (me). Seats go
    // CLOCKWISE as the seat index increases (which is the action order), so the
    // turn visibly moves clockwise like a real table. (-sin mirrors the x axis:
    // bottom -> left -> top -> right.)
    const angle = (i / n) * 2 * Math.PI;
    const x = 50 - rx * Math.sin(angle);
    const y = 50 + ry * Math.cos(angle);

    const seat = document.createElement("div");
    seat.className = "seat";
    seat.style.left = x + "%";
    seat.style.top = y + "%";
    if (p.id === myId) seat.classList.add("me");
    if (p.id === state.to_act) seat.classList.add("active");
    if (p.folded && !p.sitting_out) seat.classList.add("folded");
    if (p.sitting_out) seat.classList.add("sitting");
    if (p.disconnected) seat.classList.add("disconnected");

    // Cards: my own (or revealed at showdown) face up, others face down.
    const hc = state.hole_count || 2;
    const cardsWrap = document.createElement("div");
    cardsWrap.className = "player-cards" + (hc >= 4 ? " four" : "");
    if (p.has_cards) {
      let hole = null;
      if (p.id === myId && priv && priv.hole && priv.hole.length) hole = priv.hole;
      else if (p.hole) hole = p.hole; // revealed at showdown
      if (hole) sortHole(hole).forEach((c) => cardsWrap.appendChild(cardEl(c, true)));
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
    if (p.disconnected) {
      act.innerHTML = '<span class="dc-badge">📶 연결 끊김</span>';
    } else if (p.sitting_out) {
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
  if (!myId) {
    hint.textContent = "👀 관전 중입니다. 상단의 '🪑 테이블에 앉기'를 눌러 참여하세요.";
  } else if (state.auto_running) {
    hint.textContent = "다음 핸드를 준비하는 중...";
  } else if (isHost) {
    hint.textContent = "상단의 ▶ 게임 시작을 눌러 진행하세요. (2명 이상 필요)";
  } else {
    hint.textContent = "방장이 게임을 시작하기를 기다리는 중...";
  }

  const myTurn = priv && priv.your_turn && priv.legal && state.auto_running;

  // Pre-action bar: only meaningful while I'm still a live contender in the hand.
  const me = state.players.find((p) => p.id === myId);
  const liveContender = state.hand_in_progress && me
    && me.in_hand && !me.folded && !me.all_in;
  if (!liveContender && preAction) { preAction = null; updatePreactionUI(); }
  // Fire a queued pre-action the moment it's my turn (once).
  if (myTurn && preAction) {
    const a = preAction; preAction = null; updatePreactionUI();
    executePreAction(a);
  }
  const showPre = liveContender && state.auto_running && !myTurn;
  $("preaction-bar").classList.toggle("hidden", !showPre);

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
  $("sound-on").checked = soundOn;                // personal, not host-gated
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
  if (document.activeElement.id !== "allow-ip") $("allow-ip").checked = !!state.allow_same_ip;
  $("allow-ip").disabled = !isHost;
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
// Download every stored hand for this room as one JSON file (for analysis).
$("replay-download").onclick = () => {
  const room = encodeURIComponent(myRoom || "main");
  const a = document.createElement("a");
  a.href = "/export?room=" + room;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
};

// Open the web stats page for this room in a new tab.
$("stats-btn").onclick = () =>
  window.open("/stats?room=" + encodeURIComponent(myRoom || "main"), "_blank");

$("replay-prev").onclick = () => { replayIndex--; renderReplayFrame(); };
$("replay-next").onclick = () => { replayIndex++; renderReplayFrame(); };

// Scrub slider: drag the thumb to jump to any action in the hand.
$("replay-scrub").oninput = (e) => {
  replayIndex = parseInt(e.target.value, 10) || 0;
  renderReplayFrame();
};

// Drag horizontally across the board area to scrub like a video timeline.
(function setupReplayDrag() {
  const board = $("replay-board");
  if (!board) return;
  let dragging = false;
  const toFrame = (clientX) => {
    if (!replayFrames.length) return;
    const r = board.getBoundingClientRect();
    if (r.width <= 0) return;
    const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    replayIndex = Math.round(frac * (replayFrames.length - 1));
    renderReplayFrame();
  };
  board.addEventListener("pointerdown", (e) => {
    dragging = true;
    try { board.setPointerCapture(e.pointerId); } catch (_) {}
    toFrame(e.clientX);
    e.preventDefault();
  });
  board.addEventListener("pointermove", (e) => { if (dragging) toFrame(e.clientX); });
  board.addEventListener("pointerup", () => { dragging = false; });
  board.addEventListener("pointercancel", () => { dragging = false; });
})();

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
      // Reserve the right number of (empty) boards from the start so the layout
      // height stays constant — otherwise Double Board's second board appears at
      // the flop and shoves the controls down mid-scrub.
      boards = Array.from({ length: e.num_boards || 1 }, () => []);
      pot = 0;
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
  const scrub = $("replay-scrub");
  if (!replayFrames.length) {
    $("replay-board").innerHTML = "";
    $("replay-players").innerHTML = '<p class="muted">왼쪽에서 핸드를 선택하세요.</p>';
    $("replay-pot").textContent = "";
    $("replay-caption").textContent = "";
    $("replay-step").textContent = "";
    if (scrub) { scrub.max = 0; scrub.value = 0; }
    return;
  }
  replayIndex = Math.max(0, Math.min(replayFrames.length - 1, replayIndex));
  const f = replayFrames[replayIndex];
  if (scrub) { scrub.max = replayFrames.length - 1; scrub.value = replayIndex; }

  renderBoards($("replay-board"), f.boards);

  $("replay-pot").textContent = `팟: ${f.pot}`;

  const pl = $("replay-players");
  pl.innerHTML = "";
  f.players.forEach((p) => {
    const row = document.createElement("div");
    row.className = "rp-row" + (p.folded ? " folded" : "");
    const cards = document.createElement("div");
    cards.className = "rp-cards";
    sortHole(p.hole || []).forEach((c) => cards.appendChild(cardEl(c, true)));
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

// ---- Prefill last name/room ----------------------------------------------
// Show the join screen on a fresh page load (so the user can change their
// nickname/room first), but pre-fill the inputs with last time's values for
// convenience. Pressing 입장 still resumes the same seat via the browser token.
// (Mid-session network drops are handled separately by onSocketClose, which
// auto-reconnects without showing this screen.)
(function prefillJoin() {
  const lastName = localStorage.getItem("lastName");
  const lastRoom = localStorage.getItem("lastRoom");
  if (lastName) $("name-input").value = lastName;
  if (lastRoom) $("room-input").value = lastRoom;
})();
