"""
[1] 의사결정 추출기 — export 이벤트 스트림에서 프리플랍 RFI 결정점을 뽑는다.

RFI(Raise First In) = 내 앞이 전부 폴드라 내가 첫 자발적 액션을 하는 스팟.
각 결정점: player, pos, hand(169표기), eff_bb, n_players, action(open/fold/limp), variant.

채점 코어와 무관하게 순수 함수 — 입력은 export dict(또는 hands 리스트), 출력은 결정점 리스트.
나중에 입력 소스만 바꾸면(여러 방 합본 등) 그대로 cross-room 누적에 재사용.
"""
RANKS = "AKQJT98765432"
RANK_VAL = {r: i for i, r in enumerate(RANKS)}  # A=0(높음) .. 2=12

def to_hand_notation(hole):
    """['As','Kd'] -> 'AKo' / ['As','Ah'] -> 'AA' / ['As','Ks'] -> 'AKs'."""
    if len(hole) != 2:
        return None
    (r1, s1), (r2, s2) = (hole[0][0], hole[0][1]), (hole[1][0], hole[1][1])
    if r1 == r2:
        return r1 + r2
    # high rank first
    if RANK_VAL[r1] > RANK_VAL[r2]:
        r1, s1, r2, s2 = r2, s2, r1, s1
    return f"{r1}{r2}{'s' if s1 == s2 else 'o'}"

def _iter_hands(export):
    """export dict({hands:[...]}) 또는 hands 리스트 또는 db flat list 모두 수용."""
    if isinstance(export, dict) and "hands" in export:
        items = export["hands"]
    elif isinstance(export, list):
        items = export
    else:
        items = []
    for h in items:
        events = h.get("events") if isinstance(h, dict) else None
        if events:
            yield h, events

def extract_rfi(export):
    out = []
    for hand, events in _iter_hands(export):
        if not events or events[0].get("type") != "start":
            continue
        start = events[0]
        variant = start.get("variant", "holdem")
        bb = start.get("bb") or 0
        players = {p["name"]: p for p in start.get("players", [])}
        n_players = len(start.get("players", []))

        # 프리플랍 액션 순서대로
        seen_voluntary = False   # 누군가 자발적으로 들어왔나(레이즈/콜)
        acted = set()
        for e in events:
            if e.get("type") != "action":
                continue
            if e.get("street") != "preflop":
                break  # 프리플랍 끝
            name = e.get("name")
            label = (e.get("label") or "")
            first = name not in acted
            is_open = label.startswith("raise") or label.startswith("all-in")
            is_limp = label == "call"
            is_check = label == "check"

            # RFI 후보: 그 사람의 첫 액션이고, 아직 아무도 자발적으로 안 들어옴
            if first and not seen_voluntary and not is_check:
                p = players.get(name, {})
                stack = p.get("stack") or 0
                action = "open" if is_open else ("limp" if is_limp else "fold")
                out.append({
                    "hand_number": hand.get("number"),
                    "variant": variant,
                    "player": name,
                    "pos": p.get("pos", ""),
                    "hole": p.get("hole", []),
                    "hand": to_hand_notation(p.get("hole", [])),
                    "eff_bb": round(stack / bb, 1) if bb else None,
                    "n_players": n_players,
                    "action": action,
                })
            if is_open or is_limp:
                seen_voluntary = True
            acted.add(name)
    return out


if __name__ == "__main__":
    import json, collections
    export = json.load(open("fake_export.json", encoding="utf-8"))
    rfi = extract_rfi(export)
    lines = [f"총 RFI 결정점: {len(rfi)}\n"]
    by_pos = collections.Counter(d["pos"] for d in rfi)
    lines.append("포지션별: " + ", ".join(f"{k}={v}" for k, v in by_pos.items()))
    by_act = collections.Counter(d["action"] for d in rfi)
    lines.append("액션별: " + ", ".join(f"{k}={v}" for k, v in by_act.items()))
    lines.append("\n샘플 20개:")
    for d in rfi[:20]:
        lines.append(f"  #{d['hand_number']} {d['player']:5} {d['pos']:4} {d['hand']:4} "
                     f"{d['eff_bb']}bb {d['n_players']}p -> {d['action']}")
    open("gto_decisions_out.txt", "w", encoding="utf-8").write("\n".join(lines))
    print(f"RFI={len(rfi)} -> gto_decisions_out.txt")
