"""
RYE 레인지 차트 PDF 일괄 추출기.

'레인지 차트' 폴더의 모든 PDF에서 169콤보 각 셀의 색 + 액션(범례 자동매핑)을 추출해
chart_db.json 으로 저장한다. 정보 손실 없이 전부 보존(색 RGB + 액션 이름).

방법(PoC 검증됨):
- 핸드 라벨 텍스트 좌표 + 셀(최소면적 사각형) 색 매칭 -> 핸드별 색
- 그리드 바깥 텍스트 라인의 색 -> 범례(색->액션 이름) 자동 추출
- 셀이 흰색이면 FOLD, 아니면 범례 액션
"""
import sys, os, glob, json, datetime
import fitz

ROOT = r"레인지 차트"
RANKS = "AKQJT98765432"

def norm(t):
    # 키릴 문자 보정 (PDF에 А/К/О 섞임)
    return (t.strip().replace("О", "O").replace("А", "A").replace("К", "K")
                     .replace("о", "o").replace("Е", "E"))

def is_hand(t):
    if len(t) == 2 and t[0] == t[1]:
        return t[0] in RANKS
    if len(t) == 3 and t[2] in "so":
        return t[0] in RANKS and t[1] in RANKS
    return False

def ckey(f):
    return ",".join(f"{c:.2f}" for c in f)

def is_white(f):
    return f is not None and all(c > 0.92 for c in f)

def extract_pdf(path):
    doc = fitz.open(path)
    page = doc[0]
    # 사각형 수집 (fill 있는 것)
    rects = []
    for d in page.get_drawings():
        f = d.get("fill")
        if f is None:
            continue
        for it in d["items"]:
            if it[0] == "re":
                r = it[1]
                rects.append((r, tuple(f)))
    # 핸드 라벨 좌표
    labels = {}
    for w in page.get_text("words"):
        t = norm(w[4])
        if is_hand(t):
            labels[t] = ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)

    def cell_color(cx, cy):
        best, ba = None, 1e18
        for r, f in rects:
            if r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1:
                a = (r.x1 - r.x0) * (r.y1 - r.y0)
                if a < ba:
                    ba, best = a, f
        return best

    # 그리드 오른쪽 끝(범례 영역 경계)
    if labels:
        gx1 = max(cx for cx, cy in labels.values())
    else:
        gx1 = 0

    # 범례: 그리드 바깥의, 셀색을 가진 텍스트 라인
    legend = {}  # ckey -> action name
    for blk in page.get_text("dict")["blocks"]:
        for line in blk.get("lines", []):
            txt = "".join(s["text"] for s in line["spans"]).strip()
            x0, y0, x1, y1 = line["bbox"]
            if not txt:
                continue
            low = txt.lower()
            if "%" in txt or "combo" in low or "raiseyour" in low or "policy" in low:
                continue
            if x0 <= gx1 + 25:   # 그리드 영역 내 텍스트(핸드 라벨 등) 제외
                continue
            f = cell_color((x0 + x1) / 2, (y0 + y1) / 2)
            if f and not is_white(f):
                legend.setdefault(ckey(f), txt)

    # 핸드별 색/액션
    hands = {}
    open_count = 0
    open_combos = 0
    for h, (cx, cy) in labels.items():
        f = cell_color(cx, cy)
        if f is None or is_white(f):
            hands[h] = {"color": None, "action": "FOLD"}
        else:
            action = legend.get(ckey(f), "COLOR:" + ckey(f))
            hands[h] = {"color": [round(c, 4) for c in f], "action": action}
            open_count += 1
            open_combos += 6 if len(h) == 2 else (4 if h[2] == "s" else 12)

    return {
        "legend": legend,
        "hands": hands,
        "open_count": open_count,
        "open_combos": open_combos,
        "label_count": len(labels),
    }

def main():
    pdfs = sorted(glob.glob(os.path.join(ROOT, "**", "*.pdf"), recursive=True))
    charts = []
    errors = []
    for p in pdfs:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        parts = rel.split("/")
        try:
            data = extract_pdf(p)
        except Exception as e:
            errors.append((rel, repr(e)))
            continue
        charts.append({
            "path": rel,
            "category": parts[0],
            "folders": parts[:-1],
            "name": os.path.splitext(parts[-1])[0],
            **data,
        })

    db = {
        "source": "Raise Your Edge (RYE) range charts",
        "extracted_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "count": len(charts),
        "charts": charts,
    }
    with open("chart_db.json", "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=1)

    # 요약 리포트
    rep = []
    rep.append(f"PDF {len(pdfs)}개 중 추출 {len(charts)}, 에러 {len(errors)}")
    bad_labels = [c for c in charts if c["label_count"] != 169]
    rep.append(f"라벨!=169 차트 {len(bad_labels)}개: {[c['path'] for c in bad_labels][:20]}")
    no_legend = [c for c in charts if not c["legend"]]
    rep.append(f"범례 0개 차트 {len(no_legend)}개: {[c['path'] for c in no_legend][:20]}")
    unknown = [c for c in charts
               if any(v["action"].startswith("COLOR:") for v in c["hands"].values())]
    rep.append(f"액션이름 못찾은 색 있는 차트 {len(unknown)}개: {[c['path'] for c in unknown][:20]}")
    rep.append("")
    rep.append("=== 카테고리별 차트 수 ===")
    from collections import Counter
    for k, v in Counter(c["category"] for c in charts).most_common():
        rep.append(f"  {k}: {v}")
    rep.append("")
    rep.append("=== OPENRAISING 오픈% (검증용) ===")
    for c in charts:
        if c["category"] == "OPENRAISING":
            pct = c["open_combos"] / 1326 * 100
            rep.append(f"  {c['path']}: {c['open_count']}핸드 {pct:.1f}%")
    if errors:
        rep.append("\n=== 에러 ===")
        for rel, e in errors:
            rep.append(f"  {rel}: {e}")
    with open("extract_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(rep))
    print(f"done. charts={len(charts)} errors={len(errors)} -> chart_db.json")

if __name__ == "__main__":
    main()
