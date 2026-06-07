"""Texas Hold'em (No-Limit) game engine for a single table.

This class is the "single source of truth" for one poker table. The web server
never decides game rules itself - it just forwards player actions here and
broadcasts whatever state this engine produces. That separation is what keeps a
multiplayer game consistent: every browser shows the same thing because there is
exactly one authority.

Lifecycle of a hand:
    waiting -> preflop -> flop -> turn -> river -> showdown -> (back to waiting)

Money model:
    p.bet       = chips this player has put in during the CURRENT betting round
    p.committed = chips this player has put in during the WHOLE hand (all rounds)
    self.pot    = total chips in the middle
Side pots are computed from `committed` only at showdown, which keeps the live
logic simple.
"""

from .cards import Deck, card_rank, RANK_NAME
from .evaluator import best_hand, best_omaha, describe, describe_full

# Variant definitions: how many hole cards each player gets and how many
# community boards are dealt. The showdown uses Omaha rules (exactly 2+3) for any
# 4-card variant. "omaha2" is Double Board Omaha: two boards, pot split per board.
VARIANTS = {
    "holdem": {"hole": 2, "boards": 1, "omaha": False, "label": "홀덤"},
    "omaha":  {"hole": 4, "boards": 1, "omaha": True,  "label": "오마하"},
    "omaha2": {"hole": 4, "boards": 2, "omaha": True,  "label": "더블보드 오마하"},
}


def _result_winners(result: dict) -> list[dict]:
    """Flatten a result event's winners across boards (handles old + new format)."""
    if result.get("board_winners") is not None:
        flat: list[dict] = []
        for bw in result["board_winners"]:
            flat.extend(bw)
        return flat
    return result.get("winners", [])


# Position names by number of players in the hand, listed in seat order from the
# small blind around to the button. Matches common 2- to 9-handed conventions.
def summarize_hand(record: dict) -> str:
    """A short human title for a finished hand, e.g. '#3  Bob +60 (Straight)'."""
    result = next((e for e in record["events"] if e["type"] == "result"), None)
    title = f"#{record['number']}"
    winners = _result_winners(result) if result else []
    if winners:
        w = winners[0]
        title = f"#{record['number']}  {w['name']} +{w['amount']}"
        hand = w.get("hand")
        if not hand and result.get("showdown"):
            hand = next((r.get("hand") for r in result.get("reveals", [])
                         if r["name"] == w["name"]), "")
        if hand:
            title += f" ({hand})"
        elif not result.get("showdown"):
            title += " (무쇼다운)"
    return title


POSITION_NAMES = {
    2: ["SB", "BB"],
    3: ["SB", "BB", "BTN"],
    4: ["SB", "BB", "UTG", "BTN"],
    5: ["SB", "BB", "UTG", "CO", "BTN"],
    6: ["SB", "BB", "UTG", "HJ", "CO", "BTN"],
    7: ["SB", "BB", "UTG", "UTG+1", "HJ", "CO", "BTN"],
    8: ["SB", "BB", "UTG", "UTG+1", "LJ", "HJ", "CO", "BTN"],
    9: ["SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"],
}


class Player:
    def __init__(self, pid: str, name: str, chips: int):
        self.id = pid
        self.name = name
        self.chips = chips
        self.sitting_out = False   # persists across hands: skip dealing but still owe blinds
        self.pending_removal = False  # left mid-hand; purge at hand end (keeps seat indices valid)
        self.reset_for_hand(dealt_in=False)

    def reset_for_hand(self, dealt_in: bool):
        self.hole: list[str] = []
        self.bet = 0            # chips in pot this round
        self.committed = 0      # chips in pot this whole hand
        self.folded = False
        self.all_in = False
        self.has_acted = False  # has acted since the last bet/raise this round
        self.in_hand = dealt_in
        self.last_action = ""   # for the UI feed ("call", "raise 40", ...)


class Game:
    MAX_PLAYERS = 9

    def __init__(self, small_blind: int = 5, big_blind: int = 10,
                 starting_chips: int = 1000):
        self.players: list[Player] = []   # seat order around the table
        self.sb = small_blind
        self.bb = big_blind
        self.starting_chips = starting_chips

        # Game variant + betting limit (host-controlled, applied next hand).
        self.variant = "holdem"           # holdem | omaha | omaha2
        self.betting = "nl"               # nl (no-limit) | pl (pot-limit)

        self.button = 0                   # seat index of the dealer button
        self.phase = "waiting"
        self.boards: list[list[str]] = [[]]   # 1 or 2 community boards
        self.deck: Deck | None = None
        self.pot = 0
        self.current_bet = 0              # highest bet to match this round
        self.min_raise = big_blind        # smallest legal raise increment
        self.to_act: int | None = None    # seat index whose turn it is
        self.hand_in_progress = False
        self.log: list[str] = []          # text feed for the UI
        self.results: list[dict] = []     # winners of the last hand (per player)
        self.board_winners: list[list[dict]] = []  # winners per board at showdown
        self.revealed: dict[str, list[str]] = {}  # pid -> hole cards at showdown
        # ---- replay recording ----
        self.history: list[dict] = []     # events of the current hand
        self.hand_log: list[dict] = []    # finished hands (for replay), most recent last
        self.hand_count = 0               # how many hands have completed
        # All-in run-out: when nobody can act, board streets are revealed one at a
        # time (paced by the Room) instead of all at once, so it's watchable.
        self.awaiting_runout = False

    # ---- variant helpers ------------------------------------------------------

    @property
    def hole_count(self) -> int:
        return VARIANTS[self.variant]["hole"]

    @property
    def num_boards(self) -> int:
        return VARIANTS[self.variant]["boards"]

    @property
    def is_omaha(self) -> bool:
        return VARIANTS[self.variant]["omaha"]

    def _eval(self, hole: list[str], board: list[str]) -> tuple:
        """Score a player's best 5-card hand on one board, per the variant's rules."""
        if self.is_omaha:
            return best_omaha(hole, board)
        return best_hand(hole + board)

    # ---- seat / player helpers ------------------------------------------------

    def _player(self, pid: str) -> Player | None:
        for p in self.players:
            if p.id == pid:
                return p
        return None

    def _seat(self, pid: str) -> int | None:
        for i, p in enumerate(self.players):
            if p.id == pid:
                return i
        return None

    def is_full(self) -> bool:
        return len(self.players) >= self.MAX_PLAYERS

    def add_player(self, pid: str, name: str, chips: int | None = None) -> Player | None:
        if self.is_full():
            return None
        start = self.starting_chips if chips is None else chips
        p = Player(pid, name, start)
        self.players.append(p)
        self.log.append(f"{name} joined the table.")
        return p

    # ---- table settings (host controlled) ------------------------------------

    def set_blinds(self, sb: int, bb: int):
        """Change blinds. Takes effect on the next hand, not mid-hand."""
        self.sb = max(0, int(sb))
        self.bb = max(1, int(bb))
        if not self.hand_in_progress:
            self.min_raise = self.bb
        self.log.append(f"Blinds set to {self.sb}/{self.bb} (next hand).")

    def set_default_stack(self, amount: int):
        """Default buy-in for players who join from now on."""
        self.starting_chips = max(1, int(amount))
        self.log.append(f"Default buy-in set to {self.starting_chips}.")

    def set_variant(self, variant: str | None, betting: str | None):
        """Change game variant / betting limit. Takes effect on the next hand."""
        if variant in VARIANTS:
            self.variant = variant
        if betting in ("nl", "pl"):
            self.betting = betting
        label = VARIANTS[self.variant]["label"]
        limit = "팟리밋" if self.betting == "pl" else "노리밋"
        self.log.append(f"게임 모드: {label} · {limit} (다음 핸드부터).")

    def adjust_stack(self, pid: str, delta: int) -> tuple[int, str | None]:
        """Add (delta>0) or remove (delta<0) chips from a player's stack.

        Only allowed between hands so it never corrupts a live pot. Returns the
        actually-applied delta (clamped) and an error string if rejected.
        """
        p = self._player(pid)
        if not p:
            return 0, "Player not found."
        if self.hand_in_progress:
            return 0, "핸드 진행 중에는 스택을 조절할 수 없습니다 (핸드 사이에만 가능)."
        delta = int(delta)
        if delta < 0:
            delta = max(delta, -p.chips)   # cannot remove more than they hold
        p.chips += delta
        verb = "added to" if delta >= 0 else "removed from"
        self.log.append(f"{abs(delta)} chips {verb} {p.name}.")
        return delta, None

    def set_sitting_out(self, pid: str, value: bool):
        """Toggle a player's sit-out. Applies from the next hand."""
        p = self._player(pid)
        if not p:
            return
        p.sitting_out = bool(value)
        self.log.append(f"{p.name} is {'sitting out' if value else 'back in'}.")

    def rebuy(self, pid: str, amount: int | None = None) -> tuple[int, str | None]:
        """Top a player back up to the default buy-in. Allowed when they are not
        currently contesting a live hand (e.g. busted out). Returns added amount."""
        p = self._player(pid)
        if not p:
            return 0, "Player not found."
        if self.hand_in_progress and p.in_hand and not p.folded:
            return 0, "핸드가 끝난 뒤에 리바이할 수 있습니다."
        amt = self.starting_chips if amount is None else max(1, int(amount))
        p.chips += amt
        p.sitting_out = False           # rebuying means you want to play again
        self.log.append(f"{p.name} rebought for {amt}.")
        return amt, None

    def remove_player(self, pid: str):
        seat = self._seat(pid)
        if seat is None:
            return
        p = self.players[seat]
        if self.hand_in_progress:
            # Do NOT shrink the seat list mid-hand: to_act / button are list
            # indices and would become stale (-> IndexError on the next timer).
            # Fold them, mark for removal, and purge when the hand ends.
            was_their_turn = (self.to_act == seat)
            if p.in_hand and not p.folded:
                p.folded = True
                p.has_acted = True
            p.pending_removal = True
            self.log.append(f"{p.name} left the table.")
            if was_their_turn:
                self._after_action()   # advance the turn just like a fold
        else:
            self.players.pop(seat)
            self.log.append(f"{p.name} left the table.")

    def _next_occupied(self, idx: int) -> int:
        """Next seat (wrapping) that has a player in the current hand."""
        n = len(self.players)
        for step in range(1, n + 1):
            j = (idx + step) % n
            if self.players[j].in_hand:
                return j
        return idx

    # ---- starting a hand ------------------------------------------------------

    def can_start(self) -> bool:
        playing = [p for p in self.players if p.chips > 0 and not p.sitting_out]
        return not self.hand_in_progress and len(playing) >= 2

    def start_hand(self) -> bool:
        # `seated` players (chips left) join the blind rotation; among them only
        # those not sitting out are actually dealt cards and play.
        seated = [p for p in self.players if p.chips > 0]
        playing = [p for p in seated if not p.sitting_out]
        if self.hand_in_progress or len(playing) < 2:
            return False

        for p in self.players:
            in_rotation = p.chips > 0
            p.reset_for_hand(dealt_in=in_rotation)
            # Sitting-out players sit in the rotation (to owe blinds) but take no
            # cards and are auto-folded, so they can only ever post a dead blind.
            if in_rotation and p.sitting_out:
                p.folded = True

        self.deck = Deck()
        self.boards = [[] for _ in range(self.num_boards)]
        self.pot = 0
        self.current_bet = 0
        self.min_raise = self.bb
        self.phase = "preflop"
        self.hand_in_progress = True
        self.awaiting_runout = False
        self.results = []
        self.board_winners = []
        self.revealed = {}
        self.log.append("--- New hand ---")

        # Move the dealer button to the next seated seat.
        self.button = self._next_occupied(self.button)

        in_hand = [i for i, p in enumerate(self.players) if p.in_hand]

        # Deal two hole cards, one at a time, only to players who are playing
        # (seated and not folded i.e. not sitting out).
        order = [i for i in self._seat_order_from(self._next_occupied(self.button))
                 if not self.players[i].folded]
        for _ in range(self.hole_count):
            for i in order:
                self.players[i].hole.append(self.deck.deal_one())

        # Begin recording this hand for replay (stacks here are pre-blind).
        pos = self.positions()
        self.history = [{
            "type": "start",
            "button": self.players[self.button].name,
            "sb": self.sb, "bb": self.bb,
            "variant": self.variant, "num_boards": self.num_boards,
            "players": [
                {"name": self.players[i].name, "seat": i,
                 "hole": list(self.players[i].hole),
                 "stack": self.players[i].chips,
                 "pos": pos.get(self.players[i].id, "")}
                for i in order
            ],
        }]

        # Post blinds. Heads-up (2 players) has special positions.
        if len(in_hand) == 2:
            sb_seat = self.button
            bb_seat = self._next_occupied(self.button)
            first_to_act = self.button            # SB/button acts first preflop
        else:
            sb_seat = self._next_occupied(self.button)
            bb_seat = self._next_occupied(sb_seat)
            first_to_act = self._next_occupied(bb_seat)  # UTG

        self._post_blind(self.players[sb_seat], self.sb, "small blind")
        self._post_blind(self.players[bb_seat], self.bb, "big blind")
        self.current_bet = self.bb
        self.min_raise = self.bb

        self._set_first_actor(first_to_act)
        return True

    def _seat_order_from(self, start: int) -> list[int]:
        """Seat indices of in-hand players, going around starting at `start`."""
        order = []
        n = len(self.players)
        for step in range(n):
            j = (start + step) % n
            if self.players[j].in_hand:
                order.append(j)
        return order

    def _post_blind(self, p: Player, amount: int, label: str):
        pay = min(amount, p.chips)
        self._commit(p, pay)
        if p.chips == 0:
            p.all_in = True
        self.log.append(f"{p.name} posts {label} {pay}.")
        self.history.append({"type": "post", "name": p.name,
                             "blind": label, "amount": pay, "pot": self.pot})

    def _commit(self, p: Player, amount: int):
        """Move chips from a player's stack into the pot."""
        p.chips -= amount
        p.bet += amount
        p.committed += amount
        self.pot += amount

    # ---- betting --------------------------------------------------------------

    def _needs_to_act(self, p: Player) -> bool:
        return p.in_hand and not p.folded and not p.all_in and not p.has_acted

    def _next_to_act(self, from_idx: int) -> int | None:
        n = len(self.players)
        for step in range(1, n + 1):
            j = (from_idx + step) % n
            if self._needs_to_act(self.players[j]):
                return j
        return None

    def _set_first_actor(self, start: int):
        """Set the opening actor of a betting round (start seat included)."""
        if self._needs_to_act(self.players[start]):
            self.to_act = start
        else:
            self.to_act = self._next_to_act(start)
        # If nobody can act (e.g. all-in situation) resolve the round immediately.
        if self.to_act is None:
            self._end_betting_round()

    def _raise_bounds(self, p: Player) -> tuple[int, int, int]:
        """Return (to_call, all_in_to, max_raise_to) for the player to act.

        max_raise_to is the highest legal raise-TO this round: the all-in amount
        in no-limit, or the pot-sized cap in pot-limit (whichever is smaller).
        Pot-limit cap = current bet + (pot after you call) = current_bet + pot + to_call.
        """
        to_call = self.current_bet - p.bet
        all_in_to = p.bet + p.chips
        if self.betting == "pl":
            cap = self.current_bet + (self.pot + to_call)
            max_to = min(cap, all_in_to)
        else:
            max_to = all_in_to
        return to_call, all_in_to, max_to

    def legal_actions(self, pid: str) -> dict | None:
        """What the player to act is allowed to do (drives the UI buttons)."""
        seat = self._seat(pid)
        if seat is None or seat != self.to_act:
            return None
        p = self.players[seat]
        to_call, all_in_to, max_to = self._raise_bounds(p)
        info = {
            "can_fold": True,
            "can_check": to_call == 0,
            "can_call": to_call > 0,
            "call_amount": min(to_call, p.chips),
            "can_raise": p.chips > to_call and max_to > self.current_bet,
            "min_raise_to": 0,
            "max_raise_to": max_to,
            "to_call": to_call,
        }
        if info["can_raise"]:
            target = self.current_bet + self.min_raise
            info["min_raise_to"] = min(target, max_to)
        return info

    def act(self, pid: str, action: str, amount: int = 0) -> str | None:
        """Apply a player's action. Returns an error string, or None on success."""
        if not self.hand_in_progress:
            return "No hand in progress."
        seat = self._seat(pid)
        if seat is None or seat != self.to_act:
            return "Not your turn."
        p = self.players[seat]
        to_call = self.current_bet - p.bet
        chips_before = p.chips

        if action == "fold":
            p.folded = True
            p.last_action = "fold"

        elif action == "check":
            if to_call != 0:
                return "Cannot check, there is a bet to call."
            p.last_action = "check"

        elif action == "call":
            if to_call == 0:
                return "Nothing to call; use check."
            pay = min(to_call, p.chips)
            self._commit(p, pay)
            if p.chips == 0:
                p.all_in = True
            p.last_action = "all-in" if p.all_in else "call"

        elif action in ("bet", "raise"):
            target = int(amount)             # raise-TO semantics (total this round)
            to_call_now, all_in_to, max_to = self._raise_bounds(p)
            if target > all_in_to:
                return "You don't have that many chips."
            if target > max_to:                # pot-limit cap
                return f"팟 리밋: 최대 {max_to}까지 올릴 수 있습니다."
            going_all_in = target == all_in_to
            raise_by = target - self.current_bet
            if raise_by <= 0:
                return "Raise must be higher than the current bet."
            # A short all-in may raise by less than the minimum; otherwise enforce it.
            if not going_all_in and raise_by < self.min_raise:
                return f"Minimum raise is to {self.current_bet + self.min_raise}."
            prev_bet = self.current_bet
            self._commit(p, target - p.bet)
            # A full-size raise reopens the betting and sets the new min raise.
            if raise_by >= self.min_raise:
                self.min_raise = raise_by
            self.current_bet = max(self.current_bet, target)
            if p.chips == 0:
                p.all_in = True
            # Everyone still in must respond to the new price.
            for o in self.players:
                if o is not p and o.in_hand and not o.folded and not o.all_in:
                    o.has_acted = False
            p.last_action = ("all-in " if p.all_in else "raise ") + str(target)
            _ = prev_bet
        else:
            return f"Unknown action: {action}"

        # Record the action for replay (street = the street it happened on).
        self.history.append({
            "type": "action", "name": p.name, "label": p.last_action,
            "paid": chips_before - p.chips, "pot": self.pot, "street": self.phase,
        })

        p.has_acted = True
        self._after_action()
        return None

    def _after_action(self):
        alive = [p for p in self.players if p.in_hand and not p.folded]
        if len(alive) == 1:
            self._win_uncontested(alive[0])
            return
        nxt = self._next_to_act(self.to_act if self.to_act is not None else 0)
        if nxt is None:
            self._end_betting_round()
        else:
            self.to_act = nxt

    def _end_betting_round(self):
        """All bets matched: clear round bets and move to the next street."""
        for p in self.players:
            p.bet = 0
            if not p.all_in:
                p.has_acted = False
        self.current_bet = 0
        self.min_raise = self.bb
        self.to_act = None
        self._next_street()

    def _deal_boards(self, n: int):
        """Deal n cards to every community board (1 board normally, 2 for omaha2)."""
        for board in self.boards:
            board.extend(self.deck.deal(n))

    def _next_street(self):
        if self.phase == "preflop":
            self.phase = "flop"
            self._deal_boards(3)
        elif self.phase == "flop":
            self.phase = "turn"
            self._deal_boards(1)
        elif self.phase == "turn":
            self.phase = "river"
            self._deal_boards(1)
        elif self.phase == "river":
            self._showdown()
            return

        shown = " | ".join(" ".join(b) for b in self.boards)
        self.log.append(f"--- {self.phase.title()}: {shown} ---")
        self.history.append({"type": "street", "street": self.phase,
                             "boards": [list(b) for b in self.boards]})

        # If at most one player can still act, no more betting is possible. Rather
        # than dealing the rest of the board instantly, pause here and let the Room
        # reveal the next street after a short delay (so an all-in is watchable).
        can_act = [p for p in self.players
                   if p.in_hand and not p.folded and not p.all_in]
        if len(can_act) <= 1:
            self.awaiting_runout = True
            self.to_act = None
            return

        self.awaiting_runout = False
        self._set_first_actor(self._next_occupied(self.button))

    def deal_next_runout(self):
        """Reveal the next board street during an all-in run-out (Room-paced).

        Calling _next_street advances one street; it either sets awaiting_runout
        again (more streets to come) or, at the river, goes to showdown.
        """
        self.awaiting_runout = False
        self._next_street()

    # ---- ending a hand --------------------------------------------------------

    def _win_uncontested(self, winner: Player):
        self.history.append({"type": "result", "showdown": False,
                             "boards": [list(b) for b in self.boards], "reveals": [],
                             "winners": [{"name": winner.name, "amount": self.pot}]})
        winner.chips += self.pot
        self.results = [{"id": winner.id, "name": winner.name,
                         "amount": self.pot, "hand": ""}]
        self.board_winners = []
        self.log.append(f"{winner.name} wins {self.pot} (everyone folded).")
        self.pot = 0
        self._finish_hand()

    def _showdown(self):
        self.phase = "showdown"
        contenders = [p for p in self.players if p.in_hand and not p.folded]
        for p in contenders:
            self.revealed[p.id] = p.hole

        nb = self.num_boards
        # Each contender's best hand on each board (Omaha or Hold'em rules).
        board_scores = [
            {p.id: self._eval(p.hole, board) for p in contenders}
            for board in self.boards
        ]

        # Split into main + side pots; then split each pot across the boards.
        pots = self._build_pots()
        payouts: dict[str, int] = {p.id: 0 for p in self.players}
        board_acc: list[dict[str, int]] = [dict() for _ in range(nb)]

        for amount, eligible_ids in pots:
            eligible = [p for p in contenders if p.id in eligible_ids]
            if not eligible:
                continue
            per = amount // nb
            rem = amount - per * nb          # odd chips from the board split
            for b in range(nb):
                share = per + (rem if b == 0 else 0)
                if share <= 0:
                    continue
                best = max(board_scores[b][p.id] for p in eligible)
                winners = [p for p in eligible if board_scores[b][p.id] == best]
                cut = share // len(winners)
                odd = share - cut * len(winners)
                for w in winners:
                    payouts[w.id] += cut
                    board_acc[b][w.id] = board_acc[b].get(w.id, 0) + cut
                if odd:  # odd chip to the first winner left of the button
                    payouts[winners[0].id] += odd
                    board_acc[b][winners[0].id] = board_acc[b].get(winners[0].id, 0) + odd

        # Apply payouts and build per-player results (for seat WIN badges).
        self.results = []
        for p in contenders:
            if payouts[p.id] > 0:
                p.chips += payouts[p.id]
                hand_desc = describe_full(board_scores[0][p.id]) if nb == 1 else ""
                self.results.append({"id": p.id, "name": p.name,
                                     "amount": payouts[p.id], "hand": hand_desc})

        # Per-board winners (names + the hand they won that board with).
        self.board_winners = []
        for b in range(nb):
            bw = []
            for pid, amt in board_acc[b].items():
                pl = self._player(pid)
                bw.append({"name": pl.name, "amount": amt,
                           "hand": describe_full(board_scores[b][pid])})
            bw.sort(key=lambda x: -x["amount"])
            self.board_winners.append(bw)
            label = f" 보드{b + 1}" if nb > 1 else ""
            for w in bw:
                self.log.append(f"{w['name']} wins {w['amount']}{label} with {w['hand']}.")

        self.history.append({
            "type": "result", "showdown": True,
            "boards": [list(b) for b in self.boards],
            "reveals": [{"name": p.name, "hole": list(p.hole),
                         "hands": [describe_full(board_scores[b][p.id]) for b in range(nb)]}
                        for p in contenders],
            "board_winners": self.board_winners,
        })
        self.pot = 0
        self._finish_hand()

    def _build_pots(self) -> list[tuple[int, set[str]]]:
        """Return [(pot_amount, {eligible player ids}), ...] from committed chips.

        Folded players' chips stay in the pots but they are never eligible to win.
        """
        contributors = [p for p in self.players if p.committed > 0]
        levels = sorted({p.committed for p in contributors})
        pots = []
        prev = 0
        for lvl in levels:
            amount = 0
            eligible: set[str] = set()
            for p in contributors:
                if p.committed >= lvl:
                    amount += lvl - prev
                    if not p.folded:
                        eligible.add(p.id)
            if amount > 0:
                pots.append((amount, eligible))
            prev = lvl
        return pots

    def _finish_hand(self):
        self.hand_in_progress = False
        self.awaiting_runout = False
        self.to_act = None
        # Now that the hand is over it is safe to drop players who left mid-hand.
        if any(p.pending_removal for p in self.players):
            self.players = [p for p in self.players if not p.pending_removal]
            if self.players:
                self.button %= len(self.players)
        if self.phase != "showdown":
            self.phase = "waiting"
        # Archive this hand for replay (keep memory bounded to the last 50).
        if self.history:
            self.hand_count += 1
            self.hand_log.append({"number": self.hand_count,
                                  "events": self.history})
            self.hand_log = self.hand_log[-50:]
            self.history = []

    def positions(self) -> dict:
        """Map of player id -> position name (UTG, CO, BTN, ...) for the live hand.

        Positions are seat-based, so a sitting-out player still occupies one.
        Returns {} between hands.
        """
        if not self.hand_in_progress:
            return {}
        parts = [i for i, p in enumerate(self.players) if p.in_hand]
        n = len(parts)
        names = POSITION_NAMES.get(n)
        if not names:
            return {}
        m = len(self.players)
        if n == 2:
            # Heads-up: the button is the small blind.
            ordered = [self.button] + [i for i in parts if i != self.button]
        else:
            # Start at the small blind (first seat after the button); the button
            # is reached last, so it lands on BTN.
            ordered = []
            for step in range(1, m + 1):
                j = (self.button + step) % m
                if j in parts:
                    ordered.append(j)
        return {self.players[seat].id: names[k] for k, seat in enumerate(ordered)}

    # ---- replay access --------------------------------------------------------

    def replay_list(self) -> list[dict]:
        """Compact list of recent hands for the replay menu (newest first)."""
        out = [{"number": rec["number"], "title": summarize_hand(rec)}
               for rec in self.hand_log[-30:]]
        out.reverse()
        return out

    def get_replay(self, number: int) -> dict | None:
        for rec in self.hand_log:
            if rec["number"] == number:
                return rec
        return None

    # ---- serialization for the wire ------------------------------------------

    def public_state(self) -> dict:
        """State everyone is allowed to see (no hidden hole cards)."""
        to_act_id = None
        if self.to_act is not None and 0 <= self.to_act < len(self.players):
            to_act_id = self.players[self.to_act].id
        return {
            "phase": self.phase,
            "boards": [list(b) for b in self.boards],
            "pot": self.pot,
            "current_bet": self.current_bet,
            "min_raise": self.min_raise,
            "big_blind": self.bb,
            "small_blind": self.sb,
            "starting_chips": self.starting_chips,
            "variant": self.variant,
            "betting": self.betting,
            "hole_count": self.hole_count,
            "num_boards": self.num_boards,
            "board_winners": self.board_winners,
            "button": (self.players[self.button].id
                       if self.players and self.button < len(self.players) else None),
            "to_act": to_act_id,
            "hand_in_progress": self.hand_in_progress,
            "runout": self.awaiting_runout,
            "results": self.results,
            "log": self.log[-30:],
            "players": self._players_public(),
        }

    def _players_public(self) -> list[dict]:
        pos = self.positions()
        out = []
        for p in self.players:
            d = self._player_public(p)
            d["position"] = pos.get(p.id, "")
            out.append(d)
        return out

    def _player_public(self, p: Player) -> dict:
        revealed = self.revealed.get(p.id)
        return {
            "id": p.id,
            "name": p.name,
            "chips": p.chips,
            "bet": p.bet,
            "committed": p.committed,
            "folded": p.folded,
            "all_in": p.all_in,
            "in_hand": p.in_hand,
            "has_cards": bool(p.hole) and p.in_hand,
            "last_action": p.last_action,
            "sitting_out": p.sitting_out,
            "hole": revealed,   # only set at showdown, otherwise None
        }

    def current_hands(self, pid: str) -> list[str]:
        """Descriptive best hand(s) for this player's live cards, one per board.

        Returns [] until a hand can be made (need >= 3 board cards), or if the
        player has folded / has no cards.
        """
        p = self._player(pid)
        if (not p or not p.hole or p.folded or not self.hand_in_progress):
            return []
        out = []
        for board in self.boards:
            if len(board) < 3:
                return []   # pre-flop: not enough board to form a 5-card hand
            out.append(describe_full(self._eval(p.hole, board)))
        return out

    def private_state(self, pid: str) -> dict:
        """The part only this player may see: their own hole cards + legal moves."""
        p = self._player(pid)
        can_rebuy = bool(p) and p.chips == 0 and not (
            self.hand_in_progress and p.in_hand and not p.folded)
        return {
            "hole": p.hole if p else [],
            "legal": self.legal_actions(pid),
            "your_turn": self.to_act is not None
            and self._seat(pid) == self.to_act,
            "can_rebuy": can_rebuy,
            "sitting_out": p.sitting_out if p else False,
            "hands": self.current_hands(pid),
        }
