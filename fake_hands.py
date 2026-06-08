"""
GTO 채점기 개발용 가짜 홀덤 핸드 생성기.
엔진(poker.game.Game)을 직접 구동해 실제 export와 동일한 이벤트 구조의 핸드를
생성하고 fake_export.json 으로 저장한다. (오마하 이력과 안 섞이게 순수 홀덤만.)

- 6명, 100bb 깊이(스택 매 핸드 리셋 → 전부 40-100BB 차트 대상).
- 액션은 단순 랜덤 정책(프리플랍 오픈/폴드/콜 섞이게) → 다양한 RFI 스팟 확보.
"""
import sys, os, json, random, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poker.game import Game

random.seed(42)
SB, BB, STACK = 5, 10, 1000   # 100bb
NAMES = ["Hero", "Bot1", "Bot2", "Bot3", "Bot4", "Bot5"]
N_HANDS = 200

g = Game(SB, BB, STACK)
for i, name in enumerate(NAMES):
    g.add_player(f"p{i}", name, STACK)

def decide(g, pid):
    la = g.legal_actions(pid)
    r = random.random()
    if la["can_raise"] and r < 0.32:
        # 오픈/레이즈: 2.5bb 정도(범위 클램프)
        target = min(la["max_raise_to"], max(la["min_raise_to"], int(g.bb * 2.5)))
        return "raise", target
    if la["can_check"]:
        return "check", 0
    if la["can_call"] and r < 0.45:
        return "call", 0
    return "fold", 0

hands_played = 0
for _ in range(N_HANDS):
    # 매 핸드 전 스택 리셋 → 항상 100bb 캐시 깊이
    for p in g.players:
        p.chips = STACK
    if not g.start_hand():
        break
    guard = 0
    while g.hand_in_progress and guard < 500:
        guard += 1
        seat = g.to_act
        if seat is None:
            break
        pid = g.players[seat].id
        action, amount = decide(g, pid)
        err = g.act(pid, action, amount)
        if err:
            # 합법 액션 폴백
            g.act(pid, "fold", 0)
    hands_played += 1

export = {
    "room": "fake_dev",
    "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
    "count": len(g.hand_log),
    "hands": g.hand_log,
}
with open("fake_export.json", "w", encoding="utf-8") as fh:
    json.dump(export, fh, ensure_ascii=False, indent=1)

# 간단 요약
n_events = sum(len(h["events"]) for h in g.hand_log)
variants = set(h["events"][0].get("variant") for h in g.hand_log if h["events"])
print(f"hands={len(g.hand_log)} events={n_events} variants={variants} -> fake_export.json")
