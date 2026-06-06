"""Texas Hold'em hand evaluation.

Given a player's 2 hole cards + up to 5 community cards (7 total), find the best
possible 5-card poker hand and return a *score* that can be compared directly:
a higher score tuple beats a lower one. Ties compare equal.

The score is a tuple: (category, kicker1, kicker2, ...)
  category 8 = straight flush
           7 = four of a kind
           6 = full house
           5 = flush
           4 = straight
           3 = three of a kind
           2 = two pair
           1 = one pair
           0 = high card
The kickers are rank values (2..14) ordered so Python's tuple comparison
naturally resolves ties the way poker rules intend.
"""

from itertools import combinations

from .cards import card_rank, card_suit

CATEGORY_NAMES = {
    8: "Straight Flush",
    7: "Four of a Kind",
    6: "Full House",
    5: "Flush",
    4: "Straight",
    3: "Three of a Kind",
    2: "Two Pair",
    1: "One Pair",
    0: "High Card",
}


def _straight_high(ranks: set[int]) -> int | None:
    """If `ranks` contains a 5-in-a-row, return the high card of that run.

    Handles the wheel (A-2-3-4-5) where the Ace plays low, so its high card is 5.
    Returns None if there is no straight.
    """
    # Ace can act as 1 for the wheel.
    if 14 in ranks:
        ranks = ranks | {1}
    ordered = sorted(ranks, reverse=True)
    run = 1
    for i in range(len(ordered) - 1):
        if ordered[i] - 1 == ordered[i + 1]:
            run += 1
            if run >= 5:
                return ordered[i + 1] + 4
        else:
            run = 1
    return None


def score_five(cards: list[str]) -> tuple:
    """Score exactly 5 cards. Returns a comparable tuple."""
    ranks = sorted((card_rank(c) for c in cards), reverse=True)
    suits = [card_suit(c) for c in cards]
    is_flush = len(set(suits)) == 1
    straight_high = _straight_high(set(ranks))

    # Count how many of each rank we have: {rank: count}
    counts: dict[int, int] = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    # Sort ranks by (count desc, rank desc) so the most important cards lead.
    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    pattern = [cnt for _, cnt in by_count]          # e.g. [3, 2] for a full house
    ordered_ranks = [rank for rank, _ in by_count]  # ranks in tiebreak order

    if is_flush and straight_high:
        return (8, straight_high)
    if pattern[0] == 4:
        return (7, ordered_ranks[0], ordered_ranks[1])
    if pattern[0] == 3 and pattern[1] == 2:
        return (6, ordered_ranks[0], ordered_ranks[1])
    if is_flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if pattern[0] == 3:
        return (3, ordered_ranks[0], ordered_ranks[1], ordered_ranks[2])
    if pattern[0] == 2 and pattern[1] == 2:
        return (2, ordered_ranks[0], ordered_ranks[1], ordered_ranks[2])
    if pattern[0] == 2:
        return (1, ordered_ranks[0], ordered_ranks[1], ordered_ranks[2], ordered_ranks[3])
    return (0, *ranks)


def best_hand(cards: list[str]) -> tuple:
    """Best 5-card score out of 5, 6, or 7 cards (checks all combinations)."""
    if len(cards) < 5:
        raise ValueError("need at least 5 cards to evaluate")
    return max(score_five(list(combo)) for combo in combinations(cards, 5))


def best_omaha(hole: list[str], board: list[str]) -> tuple:
    """Best Omaha hand: EXACTLY 2 of the hole cards + EXACTLY 3 of the board.

    This is the rule that makes Omaha different from Hold'em - you cannot use one
    or zero hole cards. With 4 hole cards and a 5-card board that is
    C(4,2) * C(5,3) = 6 * 10 = 60 combinations to check.
    """
    if len(board) < 3:
        raise ValueError("need at least 3 board cards to evaluate Omaha")
    best = None
    for two in combinations(hole, 2):
        for three in combinations(board, 3):
            s = score_five(list(two) + list(three))
            if best is None or s > best:
                best = s
    return best


def describe(score: tuple) -> str:
    """Human-readable category name for a score tuple."""
    return CATEGORY_NAMES.get(score[0], "Unknown")
