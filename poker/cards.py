"""Card and Deck primitives for Texas Hold'em.

Cards are represented as short strings like "As" (Ace of spades), "Td" (Ten of
diamonds), "2c" (Two of clubs). This keeps them JSON-friendly for the wire.
"""

import random

RANKS = "23456789TJQKA"          # index 0..12 -> rank value 2..14
SUITS = "shdc"                    # spades, hearts, diamonds, clubs

RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}
RANK_NAME = {
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9",
    10: "10", 11: "J", 12: "Q", 13: "K", 14: "A",
}


def card_rank(card: str) -> int:
    """Return numeric rank (2..14) of a 2-char card string."""
    return RANK_VALUE[card[0]]


def card_suit(card: str) -> str:
    return card[1]


def make_deck() -> list[str]:
    return [r + s for r in RANKS for s in SUITS]


class Deck:
    """A shuffled deck you can deal from."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.cards = make_deck()
        self.rng.shuffle(self.cards)

    def deal(self, n: int = 1) -> list[str]:
        dealt = self.cards[:n]
        self.cards = self.cards[n:]
        return dealt

    def deal_one(self) -> str:
        return self.deal(1)[0]
