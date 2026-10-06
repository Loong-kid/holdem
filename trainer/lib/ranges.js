// ranges.json 판정기 (JS). lib/ranges.py 와 같은 로직 — build/check.py 가 두 판정기를 대조한다.
//
//   const db = Ranges.index(await (await fetch("ranges.json")).json());
//   const spot = db.byId["open/openraising_40_100bb/co"];
//   const r = Ranges.resolve(spot, "AJo", { stack: 55 });     // {act:{raise:1}, ...} 또는 null(기록 없음)
//   const f = Ranges.followup(spot, "AJo", r, "vs_raise", { stack: 55, pos: "ip" });
//
// null = 차트가 그 상황을 말하지 않음 -> 출제/채점하지 말 것.
(function (root) {
  const RANKS = "AKQJT98765432";
  const HANDS = [];
  for (let i = 0; i < 13; i++)
    for (let j = 0; j < 13; j++)
      HANDS.push(i === j ? RANKS[i] + RANKS[j]
        : i < j ? RANKS[i] + RANKS[j] + "s" : RANKS[j] + RANKS[i] + "o");

  const CMP = { lt: (a, b) => a < b, le: (a, b) => a <= b, gt: (a, b) => a > b, ge: (a, b) => a >= b };
  const has = (v) => v !== null && v !== undefined;

  // true / false / null(판정에 필요한 정보가 ctx 에 없음)
  function match(when, ctx) {
    if (!when) return true;
    if (when.hands && !when.hands.includes(ctx.hand)) return false;
    if (when.after && ctx.after !== when.after) return false;
    if (when.stack) {
      if (!has(ctx.stack)) return null;
      const [lo, hi] = when.stack;
      if ((has(lo) && ctx.stack < lo) || (has(hi) && ctx.stack >= hi)) return false;
    }
    if (when.size) {
      if (!has(ctx.size)) return null;
      if (!CMP[when.size[0]](ctx.size, when.size[1])) return false;
    }
    if (when.pos) {
      if (!has(ctx.pos)) return null;
      if (ctx.pos !== when.pos) return false;
    }
    return true;
  }

  function first(rules, ctx) {
    for (const rule of rules || []) {
      const m = match(rule.when, ctx);
      if (m === null) return null;
      if (m) return rule.act ? rule : null;
    }
    return null;
  }

  function resolve(spot, hand, { stack = null, size = null } = {}) {
    if (!has(stack) && spot.stack && spot.stack[0] === spot.stack[1]) stack = spot.stack[0];
    if (!has(size)) size = spot.open_size;
    const idx = spot.grid[hand];
    if (idx < 0) return { act: { [spot.white]: 1 } };
    return first(spot.legend[idx].rules, { hand, stack, size });
  }

  function followup(spot, hand, firstRule, situation, { stack = null, size = null, pos = null, after = null } = {}) {
    if (!firstRule || !firstRule.then) return null;
    if (!has(after)) after = bestActions(firstRule)[0];
    return first(firstRule.then[situation], { hand, stack, size, pos, after });
  }

  function bestActions(rule) {
    if (!rule) return [];
    const top = Math.max(...Object.values(rule.act));
    return Object.keys(rule.act).filter((a) => rule.act[a] === top);
  }

  function index(db) {
    db.byId = Object.fromEntries(db.spots.map((s) => [s.id, s]));
    return db;
  }

  const api = { HANDS, resolve, followup, bestActions, index };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Ranges = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
