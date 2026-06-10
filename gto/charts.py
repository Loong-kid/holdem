"""
[2] 차트 제공자 — chart_db.json의 OPENRAISING 차트를 (스택대, 포지션)으로 룩업.

여기가 '기준 전략 플러그인'. 1단계는 RYE OPENRAISING(RFI). 나중에 이 모듈만
갈아끼우면 [1]추출·[3]채점·[4]리포트 그대로 재사용(포스트플랍 솔버 등으로 확장).
"""
import json, os

# 우리 게임 포지션(POSITION_NAMES) -> 차트 파일명 포지션 토큰
POS_MAP = {
    "UTG": "UTG", "UTG+1": "UTG1", "UTG+2": "UTG2",
    "LJ": "MP",  # 차트는 MP, 우리는 LJ
    "HJ": "HJ", "CO": "CO", "BTN": "BTN", "SB": "SB", "BB": "BB",
}

def categorize_3bet(action_text):
    """3bet 차트의 셀 액션 텍스트 -> 허용 히어로액션 집합 {'3bet','call','fold'}.
    혼합 셀(예: '3B / Fold', 'Flat / 3B Bluff')은 여러 액션 허용."""
    if not action_text or action_text == "FOLD":
        return {"fold"}
    t = action_text.lower()
    s = set()
    if any(k in t for k in ("3b", "jam", "all in", "all-in", "shove", "broke")):
        s.add("3bet")
    if "flat" in t:
        s.add("call")
    if "fold" in t:
        s.add("fold")
    return s or {"3bet"}

def cat_label(actions):
    """허용집합 -> 매트릭스 표시용 대표 범주(3bet 우선, 그다음 call)."""
    if "3bet" in actions:
        return "3bet"
    if "call" in actions:
        return "call"
    return "fold"

def categorize_vs3bet(action):
    """OPENRAISING 셀 색(=오픈 후 vs-3bet 플랜) -> 허용 액션 {4bet,call,fold}.
    빈 집합이면 채점 불가(Openraise만 표기·림프 등) -> skip. 흰색(FOLD)은 애초에 오픈 안 함."""
    if not action or action == "FOLD":
        return set()
    t = action.lower()
    s = set()
    if "fold vs 3b" in t:
        s.add("fold")
    if "call" in t and "3b" in t:        # Call 3B, Call Vs 3B, CALL 3B ...
        s.add("call")
    if "4b" in t or "jam" in t:           # 4B Value/Bluff/Jam, Openjam ...
        s.add("4bet")
    if "limp" in t:
        s.add("limp")
    return s

def categorize_vs4bet(action):
    """FLATTING 셀 색(=3벳 후 vs-4bet 플랜) -> 허용 액션 {call,allin,fold}.
    명시된 것만 채점(3B+Call4B / Broke·All In / .../Fold). 그 외(3B Value 단독 등)는 빈집합 -> skip."""
    if not action or action == "FOLD":
        return set()
    t = action.lower()
    s = set()
    if "call 4b" in t:
        s.add("call")
    if "broke" in t or "all in" in t or "jam" in t:
        s.add("allin")
    if "fold" in t:                       # '3B / Fold', '.../Fold'
        s.add("fold")
    return s
# 채점 대상 포지션. 비블라인드(UTG~BTN)는 OPENRAISING 차트로, SB는 헤즈업일 때만
# HU 차트로 채점(3인+ 폴드-투-SB는 아직 보류). BB는 RFI가 아니라 추출 단계에서 제외됨.
SCORABLE_POS = {"UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN", "SB"}

HU_FROM_SB = "25BB+ FROM SB"   # 헤즈업 SB(버튼) 오픈 RFI 차트 폴더

def stack_tier(eff_bb):
    if eff_bb is None:
        return None
    if eff_bb >= 40:
        return "40-100BB"
    if eff_bb >= 20:
        return "20-40BB"
    return "10-20BB"

def hu_tier(eff_bb):
    """헤즈업 SB 오픈 차트 스택대 (HU/25BB+ FROM SB/*). <25bb는 보류(숏스택 전략 별도)."""
    if eff_bb is None:
        return None
    if eff_bb >= 50:
        return "50BB+"
    if eff_bb >= 35:
        return "35-50BB"
    if eff_bb >= 25:
        return "25-35BB"
    return None

def tier_3bet(eff_bb):
    """FLATTING & 3BETTING 스택대 (20BB / 30-40BB / 40-50BB / 50BB+)."""
    if eff_bb is None:
        return None
    if eff_bb >= 50:
        return "50BB+"
    if eff_bb >= 40:
        return "40-50BB"
    if eff_bb >= 30:
        return "30-40BB"
    return "20BB"

def _mu_token(tok):
    """매치업 포지션 토큰 정규화 (TBN 오타 -> BTN)."""
    tok = tok.strip().upper()
    return "BTN" if tok == "TBN" else tok

def _name_pos_token(name):
    """'OR 40-100BB BU' -> 'BTN', 'OR 20-40BB UTG1' -> 'UTG1' ..."""
    tok = name.split()[-1].upper()
    if tok in ("BU",):
        return "BTN"
    if tok.startswith("SB"):  # SB, SB-MIXED
        return "SB"
    return tok

class ChartProvider:
    def __init__(self, db_path="chart_db.json"):
        db = json.load(open(db_path, encoding="utf-8"))
        # (tier, pos_token) -> hands dict  (단순 OR 차트만; 특수/전략 차트 제외)
        self.index = {}
        for c in db["charts"]:
            if c["category"] != "OPENRAISING":
                continue
            folders = c["folders"]
            if len(folders) < 2:
                continue
            tier = folders[1]
            if tier not in ("10-20BB", "20-40BB", "40-100BB"):
                continue  # 'SB 40BB+ STRATEGIES' 등 특수 폴더 제외
            name = c["name"]
            if " VS " in name.upper():
                continue  # 'BU VS AGRR...' 같은 상대별 특수 제외
            ptok = _name_pos_token(name)
            self.index[(tier, ptok)] = c["hands"]

        # 헤즈업 SB 오픈 차트: HU/25BB+ FROM SB/{25-35BB|35-50BB|50BB+}
        self.hu_sb = {}
        for c in db["charts"]:
            if c["category"] == "HU" and len(c["folders"]) >= 2 \
                    and c["folders"][1] == HU_FROM_SB:
                self.hu_sb[c["name"]] = c["hands"]   # name = '25-35BB' 등

        # 3bet/플랫 차트: (tier, hero_tok, opener_tok) -> hands. name='BB VS SB' 등.
        self.threebet = {}
        for c in db["charts"]:
            if c["category"] != "FLATTING _ 3BETTING" or len(c["folders"]) < 2:
                continue
            tier = c["folders"][1]
            if " VS " not in c["name"].upper():
                continue
            hero, opener = c["name"].upper().split(" VS ", 1)
            self.threebet[(tier, _mu_token(hero), _mu_token(opener))] = c["hands"]

    def lookup(self, pos, eff_bb, n_players=None):
        """포지션·유효스택·인원 -> (chart_hands, tier_label, ptok) 또는 None.
        SB는 헤즈업(2인)일 때만 HU 차트로 채점; 3인+ SB는 None(보류)."""
        if pos == "SB":
            if n_players != 2:
                return None
            t = hu_tier(eff_bb)
            if t is None:
                return None
            hands = self.hu_sb.get(t)
            return (hands, "HU " + t, "SB") if hands else None
        tier = stack_tier(eff_bb)
        ptok = POS_MAP.get(pos)
        if tier is None or ptok is None:
            return None
        hands = self.index.get((tier, ptok))
        if hands is None:
            return None
        return hands, tier, ptok

    def lookup_3bet(self, hero_pos, opener_pos, eff_bb):
        """내 포지션·오프너 포지션·유효스택 -> (chart_hands, tier, 'HERO vs OPENER') 또는 None."""
        tier = tier_3bet(eff_bb)
        hero = POS_MAP.get(hero_pos)
        opener = POS_MAP.get(opener_pos)
        if tier is None or hero is None or opener is None:
            return None
        hands = self.threebet.get((tier, hero, opener))
        if hands is None:
            return None
        return hands, tier, f"{hero} vs {opener}"

    def lookup_vs3bet(self, opener_pos, eff_bb):
        """내 오픈 후 상대 3벳에 대한 대응 차트. 내가 오픈했으니 OPENRAISING {내pos} 재활용.
        반환 (hands, tier, ptok) — 각 핸드 action을 categorize_vs3bet로 해석."""
        tier = stack_tier(eff_bb)
        ptok = POS_MAP.get(opener_pos)
        if tier is None or ptok is None:
            return None
        hands = self.index.get((tier, ptok))
        return (hands, tier, ptok) if hands else None

    def lookup_vs4bet(self, hero_pos, opener_pos, eff_bb):
        """내 3벳 후 상대 4벳에 대한 대응 차트. FLATTING {내pos} vs {오프너pos} 재활용.
        각 핸드 action을 categorize_vs4bet로 해석."""
        tier = tier_3bet(eff_bb)
        hero = POS_MAP.get(hero_pos)
        opener = POS_MAP.get(opener_pos)
        if tier is None or hero is None or opener is None:
            return None
        hands = self.threebet.get((tier, hero, opener))
        return (hands, tier, f"{hero} vs {opener}") if hands else None

    def available(self):
        return (sorted(self.index.keys())
                + ["HU SB:" + t for t in sorted(self.hu_sb)]
                + ["3B " + "/".join(k) for k in sorted(self.threebet)])

    def library(self):
        """채점과 무관하게 참고용으로 보여줄 전체 차트 목록(레인지 매트릭스 포함)."""
        out = []
        for (tier, ptok), hands in self.index.items():
            out.append({"kind": "rfi", "group": "오픈 (RFI)", "tier": tier, "pos": ptok,
                        "label": f"{ptok} · {tier}",
                        "actions": {h: "open" for h, c in hands.items() if c["action"] != "FOLD"}})
        for tier, hands in self.hu_sb.items():
            out.append({"kind": "rfi", "group": "오픈 (헤즈업 SB)", "tier": "HU " + tier, "pos": "SB",
                        "label": f"SB 헤즈업 · {tier}",
                        "actions": {h: "open" for h, c in hands.items() if c["action"] != "FOLD"}})
        for (tier, hero, opener), hands in self.threebet.items():
            out.append({"kind": "vs_raise", "group": "vs레이즈 (3벳/콜)", "tier": tier,
                        "pos": f"{hero} vs {opener}", "label": f"{hero} vs {opener} · {tier}",
                        "actions": {h: cat_label(categorize_3bet(c["action"]))
                                    for h, c in hands.items()
                                    if categorize_3bet(c["action"]) != {"fold"}}})
        return out


if __name__ == "__main__":
    cp = ChartProvider()
    print("인덱싱된 (tier,pos):")
    for k in cp.available():
        print("  ", k)
