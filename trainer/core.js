// 레인지 트레이너 — 게임 규칙(출제·채점·진행 기록). 화면과 무관한 순수 로직이라 node 로도 테스트한다.
(function (root) {
  const R = root.Ranges || (typeof require !== "undefined" ? require("./lib/ranges.js") : null);
  const EVM = root.EV || (typeof require !== "undefined" ? require("./ev.js") : null);

  // 9인 테이블, 프리플랍 액션 순서
  const POS_ORDER = ["UTG", "UTG1", "UTG2", "MP", "HJ", "CO", "BTN", "SB", "BB"];
  const POS_LABEL = { UTG: "UTG", UTG1: "UTG+1", UTG2: "UTG+2", MP: "MP", HJ: "HJ", CO: "CO", BTN: "BTN", SB: "SB", BB: "BB" };
  const ACT_KO = { fold: "폴드", check: "체크", call: "콜", limp: "림프", raise: "레이즈", jam: "올인", other: "리잼 안 함" };
  const RANKS = "AKQJT98765432";

  const STAGES = [
    {
      id: "open", name: "풀링 오픈", desc: "앞사람이 전부 폴드했다. 오픈할까?",
      filters: [["all", "전체"], ["10_20", "10-20bb"], ["20_40", "20-40bb"], ["40_100", "40-100bb"]],
      match: (s, f) => /^open\/openraising_(10_20|20_40|40_100)bb\//.test(s.id) && !s.profile && s.hero !== "SB"
        && (f === "all" || s.id.startsWith(`open/openraising_${f}bb/`)),
      unlock: null,
    },
    {
      id: "push", name: "10bb 푸시/폴드", desc: "10bb 남았다. 올인할까, 폴드할까?",
      filters: [["all", "전체"]],
      match: (s) => s.id.startsWith("open/pushing_ranges_10bb/"),
      unlock: { stage: "open", n: 50, acc: 0.7 },
    },
    {
      id: "vsopen", name: "오픈에 대응", desc: "앞에서 누가 오픈했다. 폴드? 콜? 3벳?",
      filters: [["all", "전체"], ["20bb", "15-30bb"], ["30_40bb", "30-40bb"], ["40_50bb", "40-50bb"], ["50bbplus", "50bb+"]],
      match: (s, f) => s.id.startsWith("vs_open/") && s.source.pdf.startsWith("FLATTING")
        && (f === "all" || s.id.startsWith(`vs_open/${f}/`)),
      unlock: { stage: "open", n: 50, acc: 0.7 },
    },
    {
      id: "rejam", name: "리잼 · 리잼콜", desc: "숏스택 올인 싸움. 틀리면 칩 EV 손실만큼 깎인다",
      filters: [["all", "전체"], ["rejam", "리잼 (내가 올인)"], ["call", "리잼콜 (상대 올인)"]],
      match: (s, f) => (s.kind === "rejam" && f !== "call") || (s.kind === "call_rejam" && f !== "rejam"),
      unlock: { stage: "vsopen", n: 50, acc: 0.65 },
    },
    {
      id: "hu", name: "헤즈업", desc: "2인 테이블. SB 오픈·림프와 BB의 대응을 연습",
      filters: [["all", "전체"], ["sb", "SB 전략"], ["raise", "BB vs 오픈"], ["limp", "BB vs 림프"]],
      match: (s, f) => s.table === "hu" && (f === "all" || f === "sb" && s.kind === "open"
        || f === "raise" && s.kind === "vs_open" || f === "limp" && s.kind === "vs_limp"),
      unlock: { stage: "open", n: 50, acc: 0.7 },
    },
    {
      id: "bvb", name: "블라인드 배틀", desc: "앞에서 모두 폴드. SB 전략과 BB의 림프 대응",
      filters: [["all", "전체"], ["sb", "SB 전략"], ["limp", "BB vs 림프"]],
      match: (s, f) => (s.table === "bvb" && f !== "sb") || (s.table === "ring" && s.kind === "open"
        && s.hero === "SB" && !s.id.startsWith("open/pushing_") && f !== "limp"),
      unlock: { stage: "open", n: 50, acc: 0.7 },
    },
    {
      id: "bb", name: "BB 디펜스", desc: "오픈 크기·상대 레인지별 방어, 멀티웨이 콜까지",
      filters: [["all", "전체"], ["size", "오픈 크기별"], ["range", "레인지별"], ["multi", "멀티웨이"]],
      match: (s, f) => s.source.pdf.startsWith("BB/") && s.kind === "vs_open"
        && !s.id.startsWith("vs_open/vs_mp_bu_co/") && (f === "all"
          || f === "range" && s.villain_range_pct != null || f === "size" && s.open_size != null
          || f === "multi" && s.id.includes("/vs_ep_")),
      unlock: { stage: "vsopen", n: 50, acc: 0.65 },
    },
    {
      id: "special", name: "BTN vs 공격적인 블라인드", desc: "블라인드 성향에 맞춘 오픈·림프·올인 전략",
      filters: [["all", "20-40bb"]],
      match: (s) => s.id === "open/openraising_20_40bb/bu_vs_aggr_bb_sb",
      unlock: { stage: "open", n: 50, acc: 0.7 },
    },
  ];

  // ---------- 진행 기록 ----------
  function newProgress() {
    return {
      v: 1, score: 0, streak: 0, bestStreak: 0, answered: 0, correct: 0,
      stages: {},        // stageId -> {n, ok}
      spots: {},         // spotId -> {n, ok}
      review: [],        // 틀린 문제 복습 {spot, hand, stack, box, due}
      settings: { ante: "bb", sound: true },
    };
  }

  function stageSpots(db, stageId, filter = "all") {
    const st = STAGES.find((s) => s.id === stageId);
    return db.spots.filter((s) => st.match(s, filter));
  }

  function isUnlocked(prog, stage) {
    if (!stage.unlock) return true;
    const s = prog.stages[stage.unlock.stage] || { n: 0, ok: 0 };
    return s.n >= stage.unlock.n && s.ok / s.n >= stage.unlock.acc;
  }

  // ---------- 출제 ----------
  function sampleStacks(spot) {
    if (!spot.stack) return [null];  // 원본에 없는 스택을 만들어내지 않는다
    const [lo, hi] = spot.stack;
    if (lo === hi) return [lo];
    const out = [];
    for (let x = lo; x < hi; x++) if (!spot.breakpoints.includes(x)) out.push(x);
    return out;
  }

  function openSize(stack) {           // 표시용 오픈 크기 (bb)
    return stack < 20 ? 2 : stack < 40 ? 2.1 : 2.3;
  }

  // 상대 오픈 크기(bb = BB의 배수). 'Flat Vs Minraise' 처럼 크기에 따라 정답이 갈리므로 문제마다 정한다
  function pickFacing(stack, rnd) {
    if (stack < 30) return rnd() < 0.5 ? 2 : 2.2;
    return rnd() < 0.3 ? 2 : 2.3;
  }

  // (스팟, 스택)의 169칸 정답 + '경계 칸'(상하좌우 이웃과 정답이 다른 칸) — 경계 근처를 많이 출제한다
  const boardCache = new Map();
  function board(spot, stack, size = null) {
    const key = spot.id + "@" + stack + "@" + size;
    if (boardCache.has(key)) return boardCache.get(key);
    const cells = {};
    R.HANDS.forEach((h) => {
      const rule = R.resolve(spot, h, { stack, size });
      cells[h] = { rule, key: rule ? R.bestActions(rule).sort().join("+") : null };
    });
    const boundary = new Set();
    for (let i = 0; i < 13; i++)
      for (let j = 0; j < 13; j++) {
        const h = R.HANDS[i * 13 + j];
        if (!cells[h].key) continue;
        for (const [di, dj] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
          const a = i + di, b = j + dj;
          if (a < 0 || b < 0 || a > 12 || b > 12) continue;
          const k2 = cells[R.HANDS[a * 13 + b]].key;
          if (k2 && k2 !== cells[h].key) { boundary.add(h); break; }
        }
      }
    const out = { cells, boundary };
    boardCache.set(key, out);
    return out;
  }

  function weighted(items, weight, rnd) {
    const ws = items.map(weight);
    let t = ws.reduce((a, b) => a + b, 0) * rnd();
    for (let i = 0; i < items.length; i++) { t -= ws[i]; if (t <= 0) return items[i]; }
    return items[items.length - 1];
  }

  function dealCards(hand, rnd) {
    const suits = "shdc".split("");
    const pick = () => suits.splice(Math.floor(rnd() * suits.length), 1)[0];
    if (hand.length === 2) return [hand[0] + pick(), hand[1] + pick()];
    if (hand[2] === "s") { const s = pick(); return [hand[0] + s, hand[1] + s]; }
    return [hand[0] + pick(), hand[1] + pick()];
  }

  // 상대가 오픈하는 스팟(오픈에 대응·리잼)은 상대 오픈 크기를 문제마다 뽑는다
  const facesOpen = (spot) => spot.kind === "vs_open" || spot.kind === "rejam";

  function pickSpotFacing(spot, stack, rnd) {
    if (!facesOpen(spot)) return null;
    if (spot.open_size != null) return spot.open_size;
    if (spot.villain_range_pct != null) return null;
    if (spot.table === "hu") return [2, 2.3, 2.7][Math.floor(rnd() * 3)];
    return pickFacing(stack, rnd);
  }

  function tableOrder(spot) { return spot.table === "hu" ? ["SB", "BB"] : POS_ORDER; }
  function heroIP(spot, villain) {
    if (spot.table === "hu") return spot.hero === "BB";
    return POS_ORDER.indexOf(spot.hero) > POS_ORDER.indexOf(villain)
      ? spot.hero !== "SB" && spot.hero !== "BB" : villain === "SB" || villain === "BB";
  }
  function raiseSize(q) {
    if (q.spot.kind === "vs_limp") return 3.5;
    const label = q.spot.legend[q.spot.grid[q.hand]]?.label || "";
    const explicit = /3bet\s*\((\d+(?:\.\d+)?)x\)/i.exec(label);
    return Math.round(q.openSize * (explicit ? Number(explicit[1]) : heroIP(q.spot, q.villain) ? 3 : 3.8) * 10) / 10;
  }
  function questionContext(spot, villain) {
    if (spot.villain_range_pct != null) return { abstract: true, callers: [], villainLabel: `오픈 레인지 ${spot.villain_range_pct}%` };
    if (spot.id.includes("/vs_ep_")) return { abstract: false, callers: spot.id.includes("4wayplus") ? ["MP", "CO"] : ["CO"], villainLabel: "UTG (얼리 포지션)" };
    return { abstract: false, callers: [], villainLabel: POS_LABEL[villain] || villain };
  }

  function buildQuestion(db, stageId, spot, stack, hand, rnd, o = {}) {
    const facing = o.facing ?? spot.open_size ?? null;
    const rule = R.resolve(spot, hand, { stack, size: facing });
    if (!rule) return null;
    // 리잼콜 차트는 상대가 'SB/BB/BTN' 처럼 여럿일 수 있다 -> 한 명 고른다
    const vs = spot.villain_range_pct != null ? ["BTN"] : spot.id.includes("/vs_ep_") ? ["UTG"]
      : spot.villain ? spot.villain.split("/") : [];
    const villain = o.villain || (vs.length ? vs[Math.floor(rnd() * vs.length)] : null);
    const q = {
      stageId, spot, stack, hand, rule, review: !!o.review, facing, villain,
      cards: dealCards(hand, rnd),
      openSize: facing || openSize(stack),     // 상대 오픈 크기, 아니면 내 오픈 크기
      choices: spot.choices,
      step: "first",
      boundary: board(spot, stack, facing).boundary.has(hand),
      ...questionContext(spot, villain),
    };
    if (spot.kind === "rejam" || spot.kind === "call_rejam") q.ev = EVM.compute(db, db.equity, q, o.ante);
    return q;
  }

  function makeQuestion(db, prog, stageId, filter, rnd = Math.random) {
    const spots = stageSpots(db, stageId, filter);
    // 복습할 차례가 된 오답이 있으면 35% 확률로 먼저
    const ids = new Set(spots.map((s) => s.id));
    const due = prog.review.filter((it) => it.due <= prog.answered && ids.has(it.spot));
    if (due.length && rnd() < 0.35) {
      const it = due[Math.floor(rnd() * due.length)];
      const spot = db.byId[it.spot];
      const facing = it.facing ?? pickSpotFacing(spot, it.stack, rnd);
      const q = buildQuestion(db, stageId, spot, it.stack, it.hand, rnd,
        { review: true, facing, villain: it.villain, ante: prog.settings.ante });
      if (q) return q;
    }
    for (let tries = 0; tries < 60; tries++) {
      // 덜 풀어봤거나 많이 틀린 스팟을 더 자주
      const spot = weighted(spots, (s) => {
        const st = prog.spots[s.id] || { n: 0, ok: 0 };
        return 1 + (st.n < 5 ? 1 : 0) + 2 * (st.n ? 1 - st.ok / st.n : 0.5);
      }, rnd);
      const stacks = sampleStacks(spot);
      const stack = stacks[Math.floor(rnd() * stacks.length)];
      const facing = pickSpotFacing(spot, stack, rnd);
      const b = board(spot, stack, facing);
      const hands = R.HANDS.filter((h) => b.cells[h].rule);
      if (!hands.length) continue;
      const hand = weighted(hands, (h) => (b.boundary.has(h) ? 6 : 1), rnd);
      const q = buildQuestion(db, stageId, spot, stack, hand, rnd, { facing, ante: prog.settings.ante });
      if (q) return q;
    }
    throw new Error("출제 실패: " + stageId);
  }

  // 후속 질문의 '경계 핸드' 여부 — 후속 차트에서 상하좌우 이웃과 정답이 다른가 (레이즈 안 하는 이웃은 제외)
  function followBoundary(q, hand, answerOf) {
    const key = (h) => {
      const r = R.resolve(q.spot, h, { stack: q.stack, size: q.facing });
      if (!r || !(r.act[q.after || "raise"] > 0)) return null;
      const f = answerOf(h, r);
      return f ? R.bestActions(f).sort().join("+") : null;
    };
    const me = key(hand), i = R.HANDS.indexOf(hand);
    for (const [di, dj] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const a = Math.floor(i / 13) + di, b = (i % 13) + dj;
      if (a < 0 || b < 0 || a > 12 || b > 12) continue;
      const k2 = key(R.HANDS[a * 13 + b]);
      if (k2 && k2 !== me) return true;
    }
    return false;
  }

  // 오픈 레이즈를 맞혔을 때 가끔 이어지는 질문: 뒤 사람이 3벳했다
  function makeFollowup(q, rnd = Math.random, action = "raise") {
    if (q.spot.kind === "vs_open") return q.abstract || q.callers?.length ? null : makeVs4bet(q);
    if (!["open", "vs_limp"].includes(q.spot.kind) || !q.rule.act[action] || !q.rule.then) return null;
    const after = tableOrder(q.spot).slice(tableOrder(q.spot).indexOf(q.spot.hero) + 1);
    if (q.spot.kind === "vs_limp") after.push(q.villain);
    const villain = after[Math.floor(rnd() * after.length)];
    const pos = heroIP(q.spot, villain) ? "ip" : "oop";
    const sizes = action === "limp" ? [2.8, 3.2, 3.8] : q.spot.table === "hu" ? [2.8, 3.4, 4.2] : [2.8, 3.4];
    const size = sizes[Math.floor(rnd() * sizes.length)];
    const hasR = !!q.rule.then.vs_raise?.length, hasJ = !!q.rule.then.vs_jam?.length;
    const situation = hasJ && (!hasR || q.stageId !== "open" && q.stack < 25 && rnd() < 0.35) ? "vs_jam" : "vs_raise";
    const heroBet = action === "limp" ? 1 : q.spot.kind === "vs_limp" ? raiseSize(q) : q.openSize;
    const threeBet = situation === "vs_jam" ? q.stack : Math.round(heroBet * size * 10) / 10;
    if (!villain || !hasR && !hasJ || threeBet >= q.stack && situation !== "vs_jam") return null;
    const ctx = { stack: q.stack, size, pos, after: action };
    const rule = R.followup(q.spot, q.hand, q.rule, situation, ctx);
    if (!rule) return null;
    return {
      ...q, step: action === "limp" ? "vsiso" : "vs3bet", parentRule: q.rule, rule, villain, pos, size, situation, after: action, heroBet,
      boundary: followBoundary({ ...q, after: action }, q.hand, (h, r) => R.followup(q.spot, h, r, situation, ctx)),
      threeBet, choices: situation === "vs_jam" ? ["fold", "call"] : ["fold", "call", "raise", "jam"],
    };
  }

  // 3벳을 맞혔을 때: 오프너가 다시 올인(얕은 스택) 또는 4벳(깊은 스택)으로 받아쳤다
  function makeVs4bet(q) {
    const then = q.rule.then || {};
    const hasJ = !!(then.vs_jam && then.vs_jam.length), hasR = !!(then.vs_raise && then.vs_raise.length);
    if (!hasJ && !hasR) return null;
    const situation = hasJ && (q.stack < 40 || !hasR) ? "vs_jam" : "vs_raise";
    const rule = R.followup(q.spot, q.hand, q.rule, situation, { stack: q.stack, size: q.facing, after: "raise" });
    if (!rule) return null;
    const vil = q.villain || q.spot.villain;
    const threeBet = raiseSize(q);
    const fourBet = situation === "vs_jam" ? q.stack : Math.round(threeBet * 2.2 * 10) / 10;
    if (threeBet >= q.stack || fourBet >= q.stack && situation !== "vs_jam") return null;
    const ctx = { stack: q.stack, size: q.facing, after: "raise" };
    return {
      ...q, step: "vs4bet", parentRule: q.rule, rule, situation, villain: vil, threeBet,
      boundary: followBoundary(q, q.hand, (h, r) => R.followup(q.spot, h, r, situation, ctx)),
      fourBet,
      choices: situation === "vs_jam" ? ["fold", "call"] : ["fold", "call", "jam"],
    };
  }

  // ---------- 채점 ----------
  function grade(q, choice) {
    const best = R.bestActions(q.rule);
    const freq = q.rule.act[choice] || 0;
    let verdict, pts, note = null;
    if (best.includes(choice)) { verdict = "correct"; pts = 100; }
    else if (freq > 0) { verdict = "mixed"; pts = 70; note = `혼합 전략 — ${Math.round(freq * 100)}% 빈도로 선택`; }
    else if (q.rule.alt && q.rule.alt[choice]) { verdict = "alt"; pts = 50; note = q.rule.alt[choice]; }
    else { verdict = "wrong"; pts = q.boundary ? -10 : -40; }
    // 칩 EV 가 있으면 실수 크기를 EV 손실로: 차트 정답의 EV - 고른 행동의 EV
    let evLoss = null;
    if (q.ev && q.step === "first") {
      const ev = q.ev.ev;
      const bestEv = Math.max(...best.filter((a) => a in ev).map((a) => ev[a]));
      if (choice in ev && isFinite(bestEv)) {
        evLoss = bestEv - ev[choice];
        if (verdict === "wrong") pts = -Math.round(Math.min(40, 10 + 15 * Math.max(0, evLoss)));
      }
    }
    // 차트에 없어서 맥락으로 채운 칸이면 그 이유도 함께 (예: '50bb 이상은 차트에 없음 — 콜·4벳 둘 다 인정')
    if (q.rule.note) note = note ? `${note} · ${q.rule.note}` : q.rule.note;
    return { verdict, pts, note, best, choice, evLoss, inferred: q.rule.src === "inferred" };
  }

  const REVIEW_GAPS = [3, 8, 20, 50];   // 맞힐 때마다 복습 간격(문제 수)을 늘린다

  function record(prog, q, g) {
    const ok = g.verdict !== "wrong";
    prog.answered += 1;
    if (ok) prog.correct += 1;
    prog.streak = ok ? prog.streak + 1 : 0;
    prog.bestStreak = Math.max(prog.bestStreak, prog.streak);
    const mult = 1 + Math.min(prog.streak, 20) * 0.05;       // 연속 정답 배율 최대 x2
    const gained = g.pts > 0 ? Math.round(g.pts * mult) : g.pts;
    prog.score = Math.max(0, prog.score + gained);
    for (const [bag, key] of [[prog.stages, q.stageId], [prog.spots, q.spot.id]]) {
      bag[key] = bag[key] || { n: 0, ok: 0 };
      bag[key].n += 1;
      if (ok) bag[key].ok += 1;
    }
    if (q.step === "first") {
      const i = prog.review.findIndex((it) => it.spot === q.spot.id && it.hand === q.hand && it.stack === q.stack
        && (it.facing || null) === (q.facing || null));
      if (!ok) {
        const it = { spot: q.spot.id, hand: q.hand, stack: q.stack, facing: q.facing || null, villain: q.villain || null,
          box: 0, due: prog.answered + REVIEW_GAPS[0] };
        if (i >= 0) prog.review[i] = it; else prog.review.push(it);
      } else if (i >= 0) {
        const it = prog.review[i];
        it.box += 1;
        if (it.box >= REVIEW_GAPS.length) prog.review.splice(i, 1);
        else it.due = prog.answered + REVIEW_GAPS[it.box];
      }
    }
    return gained;
  }

  // ---------- 설명 문장 ----------
  // 같은 raise 라도 상황마다 이름이 다르다: 오픈 레이즈 / 3벳 / 4벳 / 5벳
  function actName(a, step, kind) {
    if (a === "raise") return step === "vsiso" ? "3벳" : step === "vs3bet" ? "4벳" : step === "vs4bet" ? "5벳"
      : kind === "vs_limp" ? "아이솔레이션 레이즈" : kind === "vs_open" ? "3벳" : kind === "rejam" ? "작은 3벳" : "오픈 레이즈";
    return ACT_KO[a] || a;
  }

  function actText(act, step, kind) {
    const keys = Object.keys(act);
    if (keys.length === 1) return actName(keys[0], step, kind);
    return keys.map((k) => `${actName(k, step, kind)} ${Math.round(act[k] * 100)}%`).join(" / ");
  }

  function condText(when) {
    if (!when) return "";
    const out = [];
    if (when.stack) {
      const [lo, hi] = when.stack;
      out.push(lo == null ? `${hi}bb 미만` : hi == null ? `${lo}bb 이상` : `${lo}~${hi}bb`);
    }
    if (when.size) {
      const [op, x] = when.size;
      out.push(`${x}배 ${{ lt: "미만", le: "이하", gt: "초과", ge: "이상" }[op]} 레이즈 상대`);
    }
    if (when.pos) out.push(when.pos === "ip" ? "포지션 있을 때" : "포지션 없을 때");
    if (when.hands) out.push(when.hands.join("·") + "만");
    if (when.after) out.push(`${ACT_KO[when.after]} 했을 때`);
    return out.join(", ");
  }

  // 한 범례 칸의 규칙 전체를 사람이 읽는 줄들로
  function explain(spot, hand) {
    const idx = spot.grid[hand];
    if (idx < 0) return { label: "(흰 칸)", lines: [`차트에 색이 없음 → ${ACT_KO[spot.white]}`] };
    const leg = spot.legend[idx];
    const vsOpen = spot.kind === "vs_open";
    const SIT = { vs_raise: vsOpen ? "4벳 받으면" : "레이즈/3벳 받으면", vs_jam: "올인 받으면" };
    const nextStep = vsOpen ? "vs4bet" : "vs3bet";
    const lines = leg.rules.map((r, i) => {
      const c = condText(r.when);
      const head = c ? c + " → " : leg.rules.length > 1 && i > 0 ? "그 외 → " : "";
      let line = head + (r.act ? actText(r.act, "first", spot.kind) : "차트에 기록 없음");
      for (const sit of ["vs_raise", "vs_jam"]) {
        if (!r.then || !r.then[sit] || !r.then[sit].length) continue;
        const sub = r.then[sit].map((t, k) => {
          const c2 = condText(t.when);
          const subStep = t.when?.after === "limp" || r.act?.limp && !r.act.raise ? "vsiso" : nextStep;
          return (c2 ? c2 + " " : k > 0 ? "그 외 " : "") + (t.act ? actText(t.act, subStep, spot.kind) : "기록 없음");
        });
        line += `  (${SIT[sit]}: ${sub.join(" · ")})`;
      }
      if (r.src === "inferred") line += "  ※맥락 추론";
      return line;
    });
    return { label: leg.label, lines };
  }

  const api = {
    POS_ORDER, POS_LABEL, ACT_KO, RANKS, STAGES,
    newProgress, stageSpots, isUnlocked, sampleStacks, board, makeQuestion, makeFollowup, pickFacing,
    pickSpotFacing, tableOrder, heroIP, raiseSize,
    grade, record, actName, actText, explain,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Core = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
