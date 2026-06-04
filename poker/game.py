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
from .evaluator import best_hand, describe


class Player:
    def __init__(self, pid: str, name: str, chips: int):
        self.id = pid
        self.name = name
        self.chips = chips
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
    def __init__(self, small_blind: int = 5, big_blind: int = 10,
                 starting_chips: int = 1000):
        self.players: list[Player] = []   # seat order around the table
        self.sb = small_blind
        self.bb = big_blind
        self.starting_chips = starting_chips

        self.button = 0                   # seat index of the dealer button
        self.phase = "waiting"
        self.community: list[str] = []
        self.deck: Deck | None = None
        self.pot = 0
        self.current_bet = 0              # highest bet to match this round
        self.min_raise = big_blind        # smallest legal raise increment
        self.to_act: int | None = None    # seat index whose turn it is
        self.hand_in_progress = False
        self.log: list[str] = []          # text feed for the UI
        self.results: list[dict] = []     # winners of the last hand
        self.revealed: dict[str, list[str]] = {}  # pid -> hole cards at showdown

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

    def add_player(self, pid: str, name: str) -> Player:
        p = Player(pid, name, self.starting_chips)
        self.players.append(p)
        self.log.append(f"{name} joined the table.")
        return p

    def remove_player(self, pid: str):
        p = self._player(pid)
        if not p:
            return
        # If they were in a live hand, treat it as a fold so the game can continue.
        if self.hand_in_progress and p.in_hand and not p.folded:
            p.folded = True
            p.has_acted = True
        self.players = [x for x in self.players if x.id != pid]
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
        ready = [p for p in self.players if p.chips > 0]
        return not self.hand_in_progress and len(ready) >= 2

    def start_hand(self) -> bool:
        eligible = [p for p in self.players if p.chips > 0]
        if self.hand_in_progress or len(eligible) < 2:
            return False

        for p in self.players:
            p.reset_for_hand(dealt_in=(p.chips > 0))

        self.deck = Deck()
        self.community = []
        self.pot = 0
        self.current_bet = 0
        self.min_raise = self.bb
        self.phase = "preflop"
        self.hand_in_progress = True
        self.results = []
        self.revealed = {}
        self.log.append("--- New hand ---")

        # Move the dealer button to the next eligible seat.
        self.button = self._next_occupied(self.button)

        in_hand = [i for i, p in enumerate(self.players) if p.in_hand]

        # Deal two hole cards each, one at a time starting left of the button.
        order = self._seat_order_from(self._next_occupied(self.button))
        for _ in range(2):
            for i in order:
                self.players[i].hole.append(self.deck.deal_one())

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

    def legal_actions(self, pid: str) -> dict | None:
        """What the player to act is allowed to do (drives the UI buttons)."""
        seat = self._seat(pid)
        if seat is None or seat != self.to_act:
            return None
        p = self.players[seat]
        to_call = self.current_bet - p.bet
        info = {
            "can_fold": True,
            "can_check": to_call == 0,
            "can_call": to_call > 0,
            "call_amount": min(to_call, p.chips),
            "can_raise": p.chips > to_call,
            "min_raise_to": 0,
            "max_raise_to": p.bet + p.chips,   # going all-in
            "to_call": to_call,
        }
        if info["can_raise"]:
            target = self.current_bet + self.min_raise
            info["min_raise_to"] = min(target, info["max_raise_to"])
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
            max_to = p.bet + p.chips
            if target > max_to:
                return "You don't have that many chips."
            going_all_in = target == max_to
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

    def _next_street(self):
        if self.phase == "preflop":
            self.phase = "flop"
            self.community += self.deck.deal(3)
        elif self.phase == "flop":
            self.phase = "turn"
            self.community += self.deck.deal(1)
        elif self.phase == "turn":
            self.phase = "river"
            self.community += self.deck.deal(1)
        elif self.phase == "river":
            self._showdown()
            return

        self.log.append(f"--- {self.phase.title()}: {' '.join(self.community)} ---")

        # If at most one player can still act, no more betting is possible -
        # deal the rest of the board straight through to showdown.
        can_act = [p for p in self.players
                   if p.in_hand and not p.folded and not p.all_in]
        if len(can_act) <= 1:
            self._next_street()
            return

        self._set_first_actor(self._next_occupied(self.button))

    # ---- ending a hand --------------------------------------------------------

    def _win_uncontested(self, winner: Player):
        winner.chips += self.pot
        self.results = [{"id": winner.id, "name": winner.name,
                         "amount": self.pot, "hand": ""}]
        self.log.append(f"{winner.name} wins {self.pot} (everyone folded).")
        self.pot = 0
        self._finish_hand()

    def _showdown(self):
        self.phase = "showdown"
        contenders = [p for p in self.players if p.in_hand and not p.folded]
        for p in contenders:
            self.revealed[p.id] = p.hole

        # Best hand for each contender (2 hole + 5 community).
        scores = {p.id: best_hand(p.hole + self.community) for p in contenders}

        # Split the pot into main + side pots based on how much each player put in.
        pots = self._build_pots()
        payouts: dict[str, int] = {p.id: 0 for p in self.players}
        for amount, eligible_ids in pots:
            eligible = [p for p in contenders if p.id in eligible_ids]
            if not eligible:
                continue
            best = max(scores[p.id] for p in eligible)
            winners = [p for p in eligible if scores[p.id] == best]
            share = amount // len(winners)
            remainder = amount - share * len(winners)
            for w in winners:
                payouts[w.id] += share
            if remainder:  # odd chip goes to first winner left of the button
                winners[0].chips  # no-op for clarity
                payouts[winners[0].id] += remainder

        self.results = []
        for p in contenders:
            if payouts[p.id] > 0:
                p.chips += payouts[p.id]
                desc = describe(scores[p.id])
                self.results.append({"id": p.id, "name": p.name,
                                     "amount": payouts[p.id], "hand": desc})
                self.log.append(f"{p.name} wins {payouts[p.id]} with {desc}.")
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
        self.to_act = None
        if self.phase != "showdown":
            self.phase = "waiting"

    # ---- serialization for the wire ------------------------------------------

    def public_state(self) -> dict:
        """State everyone is allowed to see (no hidden hole cards)."""
        to_act_id = None
        if self.to_act is not None and 0 <= self.to_act < len(self.players):
            to_act_id = self.players[self.to_act].id
        return {
            "phase": self.phase,
            "community": self.community,
            "pot": self.pot,
            "current_bet": self.current_bet,
            "min_raise": self.min_raise,
            "big_blind": self.bb,
            "small_blind": self.sb,
            "button": (self.players[self.button].id
                       if self.players and self.button < len(self.players) else None),
            "to_act": to_act_id,
            "hand_in_progress": self.hand_in_progress,
            "results": self.results,
            "log": self.log[-30:],
            "players": [self._player_public(p) for p in self.players],
        }

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
            "hole": revealed,   # only set at showdown, otherwise None
        }

    def private_state(self, pid: str) -> dict:
        """The part only this player may see: their own hole cards + legal moves."""
        p = self._player(pid)
        return {
            "hole": p.hole if p else [],
            "legal": self.legal_actions(pid),
            "your_turn": self.to_act is not None
            and self._seat(pid) == self.to_act,
        }
