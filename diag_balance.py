"""Reconcile analyze_hands' displayed net-sum vs true chip balance, per hand."""
import json, sys
from collections import defaultdict
from pathlib import Path
import analyze_hands as ah

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
hands = data["hands"] if isinstance(data, dict) else data

grand_rows = 0        # sum of net over analyze_hands rows (what the table shows)
grand_true = 0        # true won-contributed over ALL names incl. ghosts
per_hand_bad = []

for h in hands:
    events = h.get("events", [])
    start = next((e for e in events if e["type"] == "start"), None)
    result = next((e for e in events if e["type"] == "result"), None)

    # true balance over EVERY name that touched the pot
    contributed = defaultdict(int)
    won = defaultdict(int)
    for e in events:
        if e["type"] == "post":
            contributed[e["name"]] += e.get("amount", 0)
        elif e["type"] == "action":
            contributed[e["name"]] += e.get("paid", 0)
    if result:
        for w in ah._winners(result):
            won[w["name"]] += w.get("amount", 0)
    true_net = sum(won.values()) - sum(contributed.values())
    grand_true += true_net

    # what analyze_hands actually rows out
    rows = ah.hand_rows(h)
    row_net = sum(r["net"] for r in rows)
    grand_rows += row_net

    if row_net != 0:
        start_names = set(p["name"] for p in start["players"]) if start else set()
        ghost_in = {nm: contributed[nm] for nm in contributed if nm not in start_names}
        ghost_win = {nm: won[nm] for nm in won if nm not in start_names}
        per_hand_bad.append((h.get("hand_number"), h.get("id"), row_net,
                             true_net, ghost_in, ghost_win, bool(result)))

print(f"grand displayed row_net = {grand_rows}  (this is the +N you see)")
print(f"grand TRUE net (all names) = {grand_true}  (should be 0)\n")
for hn, hid, rn, tn, gi, gw, hasres in per_hand_bad:
    print(f"hand#{hn} id={hid} row_net={rn} true_net={tn} result={hasres}")
    if gi: print(f"    ghost contributed (not dealt in): {gi}")
    if gw: print(f"    ghost WON (not dealt in): {gw}")
