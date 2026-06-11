"""
[3] 편차 채점기 + [4] 집계 리포트 — RFI 결정점을 RYE 차트와 비교.

verdict:
  ok_open    내 오픈 = 차트도 오픈   ✅
  ok_fold    내 폴드 = 차트도 폴드   ✅
  too_loose  내 오픈, 차트는 폴드     ⚠️ 너무 루즈(레인지 밖 오픈)
  too_tight  내 폴드, 차트는 오픈     ⚠️ 너무 타이트(오픈 놓침)
  limp       림프(RFI 비표준 액션)    ⚠️
  skip:...   채점 불가(변종/포지션/스택/차트없음)
"""
from .decisions import (extract_rfi, extract_vs_raise, extract_vs3bet, extract_vs4bet,
                        extract_vs_sblimp)
from .charts import (ChartProvider, SCORABLE_POS, categorize_3bet, cat_label,
                     categorize_vs3bet, categorize_vs4bet, categorize_sblimp)

# 액션 공격성 순서 — 내 액션이 권장보다 위/아래면 과공격/소극으로 판정
VS_LADDER = ["fold", "call", "4bet", "allin"]
SBLIMP_LADDER = ["fold", "check", "raise", "allin"]   # BB의 SB림프 대응(폴드=공짜플롭 포기=최소극)

def _grade(action, allowed, ladder=VS_LADDER):
    """내 액션 vs 허용집합 -> ok_<action> / too_aggro / too_passive / wrong / None(skip)."""
    if not allowed:
        return None
    if action in allowed:
        return "ok_" + action
    if action not in ladder:
        return "wrong"
    mr = ladder.index(action)
    recs = [ladder.index(a) for a in allowed if a in ladder]
    if not recs:
        return "wrong"
    if mr > max(recs):
        return "too_aggro"      # 권장보다 공격적
    if mr < min(recs):
        return "too_passive"    # 권장보다 소극적
    return "wrong"

def _vs_label(allowed):
    """매트릭스 표시용 대표 범주(공격성 높은 것 우선)."""
    for a in ("allin", "4bet", "call", "fold"):
        if a in allowed:
            return a
    return None

def score_decision(d, cp):
    if d["variant"] != "holdem":
        return "skip:variant"
    if d["pos"] not in SCORABLE_POS:
        return "skip:pos"   # BB는 RFI 아님(추출서 제외), 그 외 비대상
    if not d["hand"]:
        return "skip:nohand"
    if d["action"] == "limp":
        return "limp"
    look = cp.lookup(d["pos"], d["eff_bb"], d["n_players"])
    if look is None:
        return "skip:nochart"   # 예: 3인+ SB(보류), 숏스택 헤즈업(<25bb)
    chart_hands, tier, ptok = look
    cell = chart_hands.get(d["hand"])
    if cell is None:
        return "skip:nohand_in_chart"
    chart_open = cell["action"] != "FOLD"
    my_open = d["action"] == "open"
    if my_open and chart_open:
        return "ok_open"
    if (not my_open) and (not chart_open):
        return "ok_fold"
    if my_open and not chart_open:
        return "too_loose"
    return "too_tight"

def score_vs_raise(d, cp):
    """단일 오프너 직면(3bet/call/fold) 채점.
    ok_3bet/ok_call/ok_fold | vs_too_tight(들어가야 하는데 폴드) |
    vs_too_loose(폴드해야 하는데 들어감) | vs_wrong(액션 종류 틀림: call↔3bet)."""
    if d["variant"] != "holdem":
        return "skip:variant"
    if not d["hand"]:
        return "skip:nohand"
    look = cp.lookup_3bet(d["pos"], d["opener_pos"], d["eff_bb"])
    if look is None:
        return "skip:nochart"   # 차트에 없는 매치업/스택
    chart_hands, tier, label = look
    cell = chart_hands.get(d["hand"])
    raw = cell["action"] if cell else "FOLD"
    allowed = categorize_3bet(raw)
    act = d["action"]                       # 3bet / call / fold
    if act in allowed:
        return "ok_" + act
    has_action = ("3bet" in allowed) or ("call" in allowed)
    if act == "fold" and has_action:
        return "vs_too_tight"
    if act in ("3bet", "call") and allowed == {"fold"}:
        return "vs_too_loose"
    return "vs_wrong"

def score_vs3bet(d, cp):
    """내 오픈 후 상대 3벳 직면 -> 4벳/콜/폴드 (OPENRAISING 색)."""
    if d["variant"] != "holdem":
        return "skip:variant"
    if not d["hand"]:
        return "skip:nohand"
    look = cp.lookup_vs3bet(d["pos"], d["eff_bb"])
    if look is None:
        return "skip:nochart"
    hands, tier, ptok = look
    cell = hands.get(d["hand"])
    allowed = categorize_vs3bet(cell["action"] if cell else "FOLD")
    allowed = {a for a in allowed if a in ("4bet", "call", "fold")}   # 림프는 범위 밖
    return _grade(d["action"], allowed) or "skip:noplan"

def score_vs4bet(d, cp):
    """내 3벳 후 상대 4벳 직면 -> 올인/콜/폴드 (FLATTING 색)."""
    if d["variant"] != "holdem":
        return "skip:variant"
    if not d["hand"]:
        return "skip:nohand"
    look = cp.lookup_vs4bet(d["pos"], d["opener_pos"], d["eff_bb"])
    if look is None:
        return "skip:nochart"
    hands, tier, label = look
    cell = hands.get(d["hand"])
    allowed = categorize_vs4bet(cell["action"] if cell else "FOLD")
    return _grade(d["action"], allowed) or "skip:noplan"

def score_vs_sblimp(d, cp):
    """헤즈업 BB가 SB 림프에 대응 -> 체크/레이즈/올인 (BB/VS SB LIMP 색)."""
    if d["variant"] != "holdem":
        return "skip:variant"
    if not d["hand"]:
        return "skip:nohand"
    look = cp.lookup_sblimp(d["eff_bb"])
    if look is None:
        return "skip:nochart"
    hands, tier, label = look
    cell = hands.get(d["hand"])
    allowed = categorize_sblimp(cell["action"] if cell else "FOLD")
    return _grade(d["action"], allowed, SBLIMP_LADDER) or "skip:noplan"

def score_export(export, db_path="chart_db.json", hero=None):
    cp = ChartProvider(db_path)
    rfi = extract_rfi(export)
    rows = []
    for d in rfi:
        if hero and d["player"] != hero:
            continue
        v = score_decision(d, cp)
        rows.append({**d, "verdict": v})
    return rows

def summarize(rows):
    import collections
    out = []
    scored = [r for r in rows if not r["verdict"].startswith("skip") and r["verdict"] != "limp"]
    ok = [r for r in scored if r["verdict"].startswith("ok")]
    loose = [r for r in scored if r["verdict"] == "too_loose"]
    tight = [r for r in scored if r["verdict"] == "too_tight"]
    limps = [r for r in rows if r["verdict"] == "limp"]
    skipped = [r for r in rows if r["verdict"].startswith("skip")]

    out.append(f"전체 RFI 결정점: {len(rows)}")
    out.append(f"채점됨: {len(scored)} | 제외: {len(skipped)} | 림프: {len(limps)}")
    if scored:
        out.append(f"정확도(GTO 일치): {len(ok)}/{len(scored)} = {len(ok)/len(scored)*100:.1f}%")
    out.append(f"⚠️ 너무 루즈(레인지 밖 오픈): {len(loose)}")
    out.append(f"⚠️ 너무 타이트(오픈 놓침): {len(tight)}")

    out.append("\n=== 포지션별 (오픈빈도 내 vs 차트권장) ===")
    by_pos = collections.defaultdict(list)
    for r in scored:
        by_pos[r["pos"]].append(r)
    order = ["UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"]
    for pos in order:
        rs = by_pos.get(pos)
        if not rs:
            continue
        my_open = sum(1 for r in rs if r["verdict"] in ("ok_open", "too_loose"))
        ll = sum(1 for r in rs if r["verdict"] == "too_loose")
        tt = sum(1 for r in rs if r["verdict"] == "too_tight")
        out.append(f"  {pos:5} n={len(rs):3} 내오픈={my_open:3} "
                   f"루즈={ll} 타이트={tt}")

    out.append("\n=== ⚠️ 리크 핸드 (루즈 오픈) ===")
    for r in loose[:25]:
        out.append(f"  #{r['hand_number']} {r['player']} {r['pos']} {r['hand']} "
                   f"{r['eff_bb']}bb -> 오픈했지만 차트는 폴드")
    out.append("\n=== ⚠️ 리크 핸드 (타이트 폴드) ===")
    for r in tight[:25]:
        out.append(f"  #{r['hand_number']} {r['player']} {r['pos']} {r['hand']} "
                   f"{r['eff_bb']}bb -> 폴드했지만 차트는 오픈")
    return "\n".join(out)


RANKS = "AKQJT98765432"

def cell_hand(i, j):
    """매트릭스 좌표 -> 169표기. 대각=페어, 상삼각(i<j)=suited, 하삼각=offsuit."""
    ri, rj = RANKS[i], RANKS[j]
    if i == j:
        return ri + rj
    if i < j:
        return ri + rj + "s"
    return rj + ri + "o"

def build_report(export, db_path="chart_db.json", hero=None):
    """UI/서버용 구조화 리포트.
    반환: {players, rows(verdict 포함, 전체), charts(등장 스팟 레인지 매트릭스)}.
    채점(차트비교)은 서버가 끝내고, player 필터/집계는 클라가 rows로 수행.
    """
    cp = ChartProvider(db_path)
    rfi = extract_rfi(export)
    rows, charts = [], {}
    players = []
    seen = set()
    def note_player(name):
        if name not in seen:
            seen.add(name); players.append(name)

    # --- RFI (오픈) ---
    for d in rfi:
        v = score_decision(d, cp)
        key = None
        if d["pos"] in SCORABLE_POS:
            look = cp.lookup(d["pos"], d["eff_bb"], d["n_players"])
            if look:
                chart_hands, tier, ptok = look
                key = f"{d['pos']}|{tier}"
                if key not in charts:
                    charts[key] = {
                        "kind": "rfi", "pos": d["pos"], "tier": tier,
                        "actions": {h: "open" for h, c in chart_hands.items()
                                    if c["action"] != "FOLD"},
                    }
        rows.append({
            "kind": "rfi", "hand_number": d["hand_number"], "player": d["player"],
            "pos": d["pos"], "opener_pos": None, "hand": d["hand"], "hole": d["hole"],
            "eff_bb": d["eff_bb"], "n_players": d["n_players"],
            "action": d["action"], "verdict": v, "chart_key": key,
        })
        note_player(d["player"])

    # --- vs-raise (3bet/call/fold) ---
    for d in extract_vs_raise(export):
        v = score_vs_raise(d, cp)
        key = None
        look = cp.lookup_3bet(d["pos"], d["opener_pos"], d["eff_bb"])
        if look:
            chart_hands, tier, label = look
            key = f"{label}|{tier}"
            if key not in charts:
                charts[key] = {
                    "kind": "vs_raise", "pos": label, "tier": tier,
                    "actions": {h: cat_label(categorize_3bet(c["action"]))
                                for h, c in chart_hands.items()
                                if categorize_3bet(c["action"]) != {"fold"}},
                }
        rows.append({
            "kind": "vs_raise", "hand_number": d["hand_number"], "player": d["player"],
            "pos": d["pos"], "opener_pos": d["opener_pos"], "hand": d["hand"],
            "hole": d["hole"], "eff_bb": d["eff_bb"], "n_players": d["n_players"],
            "action": d["action"], "verdict": v, "chart_key": key,
        })
        note_player(d["player"])

    # --- vs-3bet (내 오픈 -> 상대 3벳 -> 4벳/콜/폴드) ---
    for d in extract_vs3bet(export):
        v = score_vs3bet(d, cp)
        key = None
        look = cp.lookup_vs3bet(d["pos"], d["eff_bb"])
        if look:
            hands, tier, ptok = look
            key = f"vs3:{d['pos']}|{tier}"
            if key not in charts:
                acts = {}
                for h, c in hands.items():
                    lab = _vs_label({a for a in categorize_vs3bet(c["action"])
                                     if a in ("4bet", "call", "fold")})
                    if lab and lab != "fold":
                        acts[h] = lab
                charts[key] = {"kind": "vs_3bet", "pos": f"{d['pos']} 오픈→3벳직면",
                               "tier": tier, "actions": acts}
        rows.append({
            "kind": "vs_3bet", "hand_number": d["hand_number"], "player": d["player"],
            "pos": d["pos"], "opener_pos": None, "hand": d["hand"], "hole": d["hole"],
            "eff_bb": d["eff_bb"], "n_players": d["n_players"],
            "action": d["action"], "verdict": v, "chart_key": key,
        })
        note_player(d["player"])

    # --- vs-4bet (내 3벳 -> 상대 4벳 -> 올인/콜/폴드) ---
    for d in extract_vs4bet(export):
        v = score_vs4bet(d, cp)
        key = None
        look = cp.lookup_vs4bet(d["pos"], d["opener_pos"], d["eff_bb"])
        if look:
            hands, tier, label = look
            key = f"vs4:{label}|{tier}"
            if key not in charts:
                acts = {}
                for h, c in hands.items():
                    lab = _vs_label(categorize_vs4bet(c["action"]))
                    if lab and lab != "fold":
                        acts[h] = lab
                charts[key] = {"kind": "vs_4bet", "pos": f"{label} 3벳→4벳직면",
                               "tier": tier, "actions": acts}
        rows.append({
            "kind": "vs_4bet", "hand_number": d["hand_number"], "player": d["player"],
            "pos": d["pos"], "opener_pos": d["opener_pos"], "hand": d["hand"],
            "hole": d["hole"], "eff_bb": d["eff_bb"], "n_players": d["n_players"],
            "action": d["action"], "verdict": v, "chart_key": key,
        })
        note_player(d["player"])

    # --- vs SB limp (헤즈업 BB가 SB 림프에 -> 체크/레이즈/올인) ---
    for d in extract_vs_sblimp(export):
        v = score_vs_sblimp(d, cp)
        key = None
        look = cp.lookup_sblimp(d["eff_bb"])
        if look:
            hands, tier, label = look
            key = f"sbl:{tier}"
            if key not in charts:
                acts = {}
                for h, c in hands.items():
                    al = categorize_sblimp(c["action"])
                    lab = "allin" if "allin" in al else ("raise" if "raise" in al
                          else ("check" if "check" in al else None))
                    if lab:
                        acts[h] = lab
                charts[key] = {"kind": "vs_sblimp", "pos": "BB vs SB림프",
                               "tier": tier, "actions": acts}
        rows.append({
            "kind": "vs_sblimp", "hand_number": d["hand_number"], "player": d["player"],
            "pos": d["pos"], "opener_pos": "SB", "hand": d["hand"], "hole": d["hole"],
            "eff_bb": d["eff_bb"], "n_players": d["n_players"],
            "action": d["action"], "verdict": v, "chart_key": key,
        })
        note_player(d["player"])

    return {"players": players, "rows": rows, "charts": charts}


if __name__ == "__main__":
    import json, sys
    export = json.load(open("fake_export.json", encoding="utf-8"))
    hero = sys.argv[1] if len(sys.argv) > 1 else None
    rows = score_export(export, hero=hero)
    rep = summarize(rows)
    open("gto_score_out.txt", "w", encoding="utf-8").write(rep)
    print(rep.split("\n")[0], "-> gto_score_out.txt")
