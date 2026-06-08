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
    "HJ": "HJ", "CO": "CO", "BTN": "BTN", "SB": "SB",
}
# 비블라인드 RFI(1단계 채점 대상). SB/BB는 1단계 보류.
SCORABLE_POS = {"UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"}

def stack_tier(eff_bb):
    if eff_bb is None:
        return None
    if eff_bb >= 40:
        return "40-100BB"
    if eff_bb >= 20:
        return "20-40BB"
    return "10-20BB"

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

    def lookup(self, pos, eff_bb):
        """우리 포지션 이름 + 유효스택 -> (chart_hands, tier, ptok) 또는 None."""
        tier = stack_tier(eff_bb)
        ptok = POS_MAP.get(pos)
        if tier is None or ptok is None:
            return None
        hands = self.index.get((tier, ptok))
        if hands is None:
            return None
        return hands, tier, ptok

    def available(self):
        return sorted(self.index.keys())


if __name__ == "__main__":
    cp = ChartProvider()
    print("인덱싱된 (tier,pos):")
    for k in cp.available():
        print("  ", k)
