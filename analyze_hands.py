"""Analyze exported Holdem hands.

Feed it the JSON you download from the site (🎬 리플레이 -> ⬇ 전체 JSON) or from
backup_db.py. It prints a per-player stat table and writes a flat per-hand CSV
you can open in Excel / pandas for deeper digging.

Usage (PowerShell):
    py analyze_hands.py holdem_main_42hands.json
    py analyze_hands.py backups\holdem_hands_20260608_120000.json

The two layers, and why:
  * The JSON keeps the FULL event stream per hand (raw fidelity = replayable,
    street-by-street). Keep this as your source of truth.
  * This script DERIVES a flat table (one row per player per hand) from it, which
    is what you actually compute poker stats on (VPIP, PFR, net chips, ...).
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_hands(path: str) -> list[dict]:
    """Accept either the site export ({room, hands:[...]}) or a flat list."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "hands" in data:
        return data["hands"]
    if isinstance(data, list):
        return data
    raise SystemExit("알 수 없는 JSON 형식입니다 (사이트 export 또는 backup_db.py 출력이어야 함).")


def _winners(result: dict) -> list[dict]:
    """Flatten winners across boards (handles single + double board formats)."""
    if result.get("board_winners") is not None:
        flat = []
        for bw in result["board_winners"]:
            flat.extend(bw)
        return flat
    return result.get("winners", [])


def hand_rows(hand: dict) -> list[dict]:
    """One row per player who was dealt into this hand."""
    events = hand.get("events", [])
    start = next((e for e in events if e["type"] == "start"), None)
    result = next((e for e in events if e["type"] == "result"), None)
    if not start:
        return []

    names = [p["name"] for p in start["players"]]
    pos = {p["name"]: p.get("pos", "") for p in start["players"]}
    hole = {p["name"]: " ".join(p.get("hole", [])) for p in start["players"]}

    contributed = defaultdict(int)   # chips put in (blinds + bets)
    won = defaultdict(int)
    vpip = defaultdict(bool)         # voluntarily put money in preflop
    pfr = defaultdict(bool)          # raised preflop
    saw_flop = defaultdict(bool)
    folded = defaultdict(bool)

    for e in events:
        t = e["type"]
        if t == "post":
            contributed[e["name"]] += e.get("amount", 0)
        elif t == "action":
            nm, label = e["name"], (e.get("label") or "")
            contributed[nm] += e.get("paid", 0)
            if e.get("street") == "preflop":
                if label.startswith(("raise", "all-in")):
                    vpip[nm] = True
                    pfr[nm] = True
                elif label.startswith("call"):
                    vpip[nm] = True
            if label.startswith("fold"):
                folded[nm] = True
        elif t == "street" and e.get("street") == "flop":
            for nm in names:
                if not folded[nm]:
                    saw_flop[nm] = True

    showdown = bool(result and result.get("showdown"))
    reached_sd = set()
    if showdown and result:
        for r in result.get("reveals", []):
            reached_sd.add(r["name"])
    if result:
        for w in _winners(result):
            won[w["name"]] += w.get("amount", 0)

    rows = []
    for nm in names:
        net = won[nm] - contributed[nm]
        rows.append({
            "room": hand.get("room", ""),
            "hand": hand.get("hand_number") or hand.get("id"),
            "player": nm,
            "pos": pos.get(nm, ""),
            "hole": hole.get(nm, ""),
            "contributed": contributed[nm],
            "won": won[nm],
            "net": net,
            "vpip": int(vpip[nm]),
            "pfr": int(pfr[nm]),
            "saw_flop": int(saw_flop[nm]),
            "showdown": int(nm in reached_sd),
            "won_at_sd": int(nm in reached_sd and won[nm] > 0),
        })
    return rows


def main():
    if len(sys.argv) < 2:
        print("사용법: py analyze_hands.py <export.json>")
        sys.exit(1)
    path = sys.argv[1]
    hands = load_hands(path)
    rows = [r for h in hands for r in hand_rows(h)]
    if not rows:
        print("분석할 핸드가 없습니다.")
        return

    # ---- per-player aggregate ----
    agg = defaultdict(lambda: defaultdict(int))
    for r in rows:
        a = agg[r["player"]]
        a["hands"] += 1
        for k in ("net", "vpip", "pfr", "saw_flop", "showdown", "won_at_sd"):
            a[k] += r[k]

    def pct(n, d):
        return f"{100 * n / d:4.0f}%" if d else "   -"

    print(f"\n파일: {path}   |   핸드 수: {len(hands)}   |   플레이어 행: {len(rows)}\n")
    header = f"{'플레이어':<14}{'핸드':>5}{'순익':>9}{'VPIP':>7}{'PFR':>7}{'WTSD':>7}{'W$SD':>7}"
    print(header)
    print("-" * len(header))
    for name, a in sorted(agg.items(), key=lambda kv: -kv[1]["net"]):
        h = a["hands"]
        print(f"{name:<14}{h:>5}{a['net']:>9}"
              f"{pct(a['vpip'], h):>7}{pct(a['pfr'], h):>7}"
              f"{pct(a['showdown'], h):>7}{pct(a['won_at_sd'], a['showdown']):>7}")
    print("\n  VPIP=프리플랍 자발적 참여율  PFR=프리플랍 레이즈율  "
          "WTSD=쇼다운까지 간 비율  W$SD=쇼다운서 이긴 비율")

    # ---- flat CSV for spreadsheet / pandas ----
    out = Path(path).with_suffix("").name + "_rows.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n행 단위 CSV 저장: {out}  (Excel/pandas로 열어서 추가 분석)")


if __name__ == "__main__":
    main()
