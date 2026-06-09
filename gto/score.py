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
from .decisions import extract_rfi
from .charts import ChartProvider, SCORABLE_POS

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
    for d in rfi:
        v = score_decision(d, cp)
        rows.append({
            "hand_number": d["hand_number"], "player": d["player"],
            "pos": d["pos"], "hand": d["hand"], "hole": d["hole"],
            "eff_bb": d["eff_bb"], "n_players": d["n_players"],
            "action": d["action"], "verdict": v,
        })
        if d["player"] not in seen:
            seen.add(d["player"]); players.append(d["player"])
        # 등장한 채점가능 스팟의 레인지 매트릭스 수집(한 번만)
        if d["pos"] in SCORABLE_POS:
            look = cp.lookup(d["pos"], d["eff_bb"], d["n_players"])
            if look:
                chart_hands, tier, ptok = look
                key = f"{d['pos']}|{tier}"
                if key not in charts:
                    charts[key] = {
                        "pos": d["pos"], "tier": tier,
                        "actions": {h: c["action"] for h, c in chart_hands.items()
                                    if c["action"] != "FOLD"},
                    }
    return {"players": players, "rows": rows, "charts": charts}


if __name__ == "__main__":
    import json, sys
    export = json.load(open("fake_export.json", encoding="utf-8"))
    hero = sys.argv[1] if len(sys.argv) > 1 else None
    rows = score_export(export, hero=hero)
    rep = summarize(rows)
    open("gto_score_out.txt", "w", encoding="utf-8").write(rep)
    print(rep.split("\n")[0], "-> gto_score_out.txt")
