// 칩 EV 계산 — 리잼(내가 올인) / 리잼콜(상대 올인에 콜).  브라우저·node 공용.
//
// 쓰는 차트 (상대 레인지를 차트에서 가져온다)
//   리잼   : 상대 오픈 레인지(OPENRAISING) 중 리잼에 콜하는 비율(CALLING REJAMS) -> 폴드 확률, 콜 받았을 때 승률
//   리잼콜 : 상대 리잼 레인지(REJAMMING) -> 승률
// 짝 차트가 없는 매치업은 가장 가까운 자리의 차트로 대신 계산하고 approx 로 표시한다.
// 승률은 169x169 표(poker-ranges/equity.json), 내 실제 카드로 상대 조합 수를 줄인다(카드 제거).
(function (root) {
  const R = root.Ranges || (typeof require !== "undefined" ? require("./lib/ranges.js") : null);
  const POS = ["UTG", "UTG1", "UTG2", "MP", "HJ", "CO", "BTN", "SB", "BB"];
  const RANKS = "AKQJT98765432";

  // 핸드 클래스의 실제 조합 중 죽은 카드와 겹치지 않는 것의 수
  function liveCombos(h, dead) {
    const suits = "shdc";
    let n = 0;
    for (const s1 of suits) for (const s2 of suits) {
      const a = h[0] + s1, b = h[1] + s2;
      if (h.length === 2 ? s1 >= s2 : h[2] === "s" ? s1 !== s2 : s1 === s2) continue;
      if (dead.includes(a) || dead.includes(b)) continue;
      n++;
    }
    return n;
  }

  function openSpot(db, pos, stack) {
    const tier = stack < 20 ? "10_20" : stack < 40 ? "20_40" : "40_100";
    let p = pos.toLowerCase();
    if (pos === "BTN" && tier === "40_100") p = "bu";
    if (pos === "SB" && tier === "20_40") p = "sb_mixed";
    return db.byId[`open/openraising_${tier}bb/${p}`] || null;
  }

  // kind 스팟 중 (hero, villain) 에 가장 가까운 것.  villain 칸은 'SB/BB/BTN' 처럼 여럿일 수 있다
  function nearest(db, kind, hero, villain) {
    let best = null, bestD = 1e9;
    for (const s of db.spots) {
      if (s.kind !== kind) continue;
      const vs = s.villain.split("/");
      const dv = Math.min(...vs.map((v) => Math.abs(POS.indexOf(v) - POS.indexOf(villain))));
      const d = Math.abs(POS.indexOf(s.hero) - POS.indexOf(hero)) * 10 + dv;
      if (d < bestD) { bestD = d; best = s; }
    }
    return best && { spot: best, approx: bestD > 0 };
  }

  // 리잼·리잼콜 차트의 'Xbb' 라벨은 'X bb 이하'라서 짝 차트는 경계값을 포함하도록 살짝 아래에서 조회한다
  const UPTO = 0.01;

  function weights(spot, stack, act) {
    const w = {};
    for (const h of R.HANDS) {
      const r = R.resolve(spot, h, { stack });
      if (r && r.act[act] > 0) w[h] = r.act[act];
    }
    return w;
  }

  // 상대 레인지(가중치) 상대 내 핸드의 승률 + 그 레인지의 (카드 제거 반영) 조합 수
  function vsRange(EQ, hand, w, dead) {
    const i = EQ.hands.indexOf(hand);
    let n = 0, e = 0;
    for (const h in w) {
      const c = liveCombos(h, dead) * w[h];
      if (!c) continue;
      n += c;
      e += c * EQ.eq[i][EQ.hands.indexOf(h)];
    }
    return { combos: n, eq: n ? e / n / 10000 : null };
  }

  const blind = (p) => (p === "SB" ? 0.5 : p === "BB" ? 1 : 0);
  const ANTE = { none: 0, bb: 1, all: 9 * 0.125 };

  // q: {spot(kind rejam|call_rejam), hand, cards, stack, villain, openSize}
  function compute(db, EQ, q, ante = "bb") {
    if (!EQ) return null;
    const S = q.stack, hero = q.spot.hero, vil = q.villain, A = ANTE[ante] || 0;
    const dead = q.cards;
    const other = A + 1.5 - blind(hero) - blind(vil);     // 둘 외의 사람이 낸 돈 (앤티·블라인드)

    if (q.spot.kind === "call_rejam") {
      // 내가 O 오픈 -> 상대 S 올인. 상대 리잼 레인지는 REJAMMING {상대} VS {나}
      const src = nearest(db, "rejam", vil, hero);
      if (!src) return null;
      const w = weights(src.spot, S - UPTO, "jam");
      const v = vsRange(EQ, q.hand, w, dead);
      if (v.eq == null) return null;
      const O = q.openSize, pot = 2 * S + other, cost = S - O;
      return {
        kind: "call_rejam", approx: src.approx, source: src.spot.title,
        ev: { fold: 0, call: v.eq * pot - cost },
        eq: v.eq, needEq: cost / pot, rangePct: v.combos / 1326,
      };
    }

    // 리잼: 상대 오픈 레인지 x 상대의 리잼콜 레인지 (CALLING REJAMS {상대} VS {나})
    const os = openSpot(db, vil, S);
    const src = nearest(db, "call_rejam", vil, hero);
    if (!os || !src) return null;
    const openW = weights(os, S, "raise");
    const callW = weights(src.spot, S - UPTO, "call");
    const both = {};
    for (const h in openW) if (callW[h]) both[h] = openW[h] * callW[h];
    const all = vsRange(EQ, q.hand, openW, dead), called = vsRange(EQ, q.hand, both, dead);
    if (!all.combos) return null;
    const fe = 1 - called.combos / all.combos;
    const hp = blind(hero), O = q.openSize;
    const p0 = other + hp + O;                           // 내가 올인하기 직전 팟
    const whenCalled = called.combos ? called.eq * (2 * S + other) - (S - hp) : 0;
    return {
      kind: "rejam", approx: src.approx, source: src.spot.title, openSource: os.title,
      ev: { other: 0, jam: fe * p0 + (1 - fe) * whenCalled },
      fe, eq: called.eq, openPct: all.combos / 1326, callPct: called.combos / 1326,
    };
  }

  const api = { compute, liveCombos, nearest, openSpot };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EV = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
