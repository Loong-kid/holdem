"""Versioned, server-private Game snapshots, independent of any hosting SDK.

These contain every player's cards AND the remaining deck. Store only on the
server; send clients Game.public_state()/private_state() instead. This codec
does not yet save Room sessions, deadlines, or connections.
"""

import json
import random

from .cards import Deck, make_deck
from .game import Game, Player, VARIANTS

VERSION = 1
GAME_FIELDS = (
    "sb", "bb", "starting_chips", "variant", "betting", "button", "phase",
    "boards", "pot", "current_bet", "min_raise", "hand_bb", "to_act",
    "hand_in_progress", "log", "results", "board_winners", "revealed",
    "history", "hand_log", "hand_count", "awaiting_runout",
)
PLAYER_FIELDS = (
    "id", "name", "chips", "sitting_out", "pending_removal", "hole", "bet",
    "committed", "folded", "all_in", "has_acted", "in_hand", "last_action",
)
CARDS = frozenset(make_deck())


class SnapshotError(ValueError):
    """Unsupported, malformed, or incomplete snapshot. Never silently reset it."""


def _require(condition, message):
    if not condition:
        raise SnapshotError(message)


def _keys(value, fields, label):
    _require(type(value) is dict and set(value) == set(fields),
             f"{label}: unexpected or missing fields")


def _clone(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as error:
        raise SnapshotError("Snapshot must contain finite JSON data") from error


def _card_list(value):
    _require(type(value) is list and all(type(c) is str and c in CARDS for c in value),
             "Invalid card list")
    return value


def _validate(data):
    _keys(data, ("version", "game", "players", "deck"), "snapshot")
    _require(type(data["version"]) is int and data["version"] == VERSION,
             "Unsupported snapshot version; explicit migration required")
    game = data["game"]
    _keys(game, GAME_FIELDS, "game")
    for key in ("sb", "button", "pot", "current_bet", "hand_count"):
        _require(type(game[key]) is int and game[key] >= 0, f"Invalid {key}")
    for key in ("bb", "starting_chips", "min_raise", "hand_bb"):
        _require(type(game[key]) is int and game[key] > 0, f"Invalid {key}")
    for key in ("hand_in_progress", "awaiting_runout"):
        _require(type(game[key]) is bool, f"Invalid {key}")
    _require(game["variant"] in tuple(VARIANTS) and game["betting"] in ("nl", "pl"),
             "Unknown game variant or betting limit")
    _require(game["phase"] in ("waiting", "preflop", "flop", "turn", "river", "showdown"),
             "Unknown phase")
    players = data["players"]
    _require(type(players) is list and len(players) <= Game.MAX_PLAYERS, "Invalid seats")
    ids, live_cards = set(), []
    for player in players:
        _keys(player, PLAYER_FIELDS, "player")
        _require(type(player["id"]) is str and bool(player["id"])
                 and player["id"] not in ids, "Invalid or duplicate player id")
        ids.add(player["id"])
        for key in ("name", "last_action"):
            _require(type(player[key]) is str, f"Invalid player {key}")
        for key in ("chips", "bet", "committed"):
            _require(type(player[key]) is int and player[key] >= 0, f"Invalid player {key}")
        for key in ("sitting_out", "pending_removal", "folded", "all_in", "has_acted", "in_hand"):
            _require(type(player[key]) is bool, f"Invalid player {key}")
        _require(player["bet"] <= player["committed"], "Bet exceeds committed chips")
        live_cards.extend(_card_list(player["hole"]))
    _require(not players or game["button"] < len(players), "Invalid button seat")
    actor = game["to_act"]
    _require(actor is None or type(actor) is int and 0 <= actor < len(players), "Invalid actor seat")
    _require(type(game["boards"]) is list and 1 <= len(game["boards"]) <= 2, "Invalid boards")
    for board in game["boards"]:
        _require(len(_card_list(board)) <= 5, "Board has too many cards")
        live_cards.extend(board)
    if data["deck"] is not None:
        live_cards.extend(_card_list(data["deck"]))
    _require(len(live_cards) == len(set(live_cards)), "Duplicate live cards")
    _require(not game["hand_in_progress"] or data["deck"] is not None, "Active hand needs a deck")
    _require(type(game["log"]) is list and all(type(s) is str for s in game["log"]), "Invalid log")
    for key in ("results", "history", "hand_log"):
        _require(type(game[key]) is list and all(type(row) is dict for row in game[key]),
                 f"Invalid {key}")
    _require(type(game["board_winners"]) is list
             and all(type(board) is list and all(type(row) is dict for row in board)
                     for board in game["board_winners"]), "Invalid board winners")
    _require(type(game["revealed"]) is dict, "Invalid revealed cards")
    for cards in game["revealed"].values():
        _card_list(cards)


def snapshot_game(game: Game) -> dict:
    """Capture detached JSON data. New engine fields require a schema decision."""
    _require(set(vars(game)) == set(GAME_FIELDS) | {"players", "deck"},
             "Game fields changed; update the snapshot schema")
    for player in game.players:
        _require(set(vars(player)) == set(PLAYER_FIELDS),
                 "Player fields changed; update the snapshot schema")
    data = _clone({
        "version": VERSION,
        "game": {key: getattr(game, key) for key in GAME_FIELDS},
        "players": [{key: getattr(player, key) for key in PLAYER_FIELDS} for player in game.players],
        "deck": game.deck.cards if game.deck is not None else None,
    })
    _validate(data)
    return data


def restore_game(snapshot: dict) -> Game:
    """Create a fresh Game without dealing or reshuffling the saved deck.

    The shuffled order, not a random seed, is authoritative for the current hand.
    Future hands continue to use the engine's existing fresh-deck behavior.
    """
    data = _clone(snapshot)
    _validate(data)
    game = Game()
    for key in GAME_FIELDS:
        setattr(game, key, data["game"][key])
    for row in data["players"]:
        player = Player(row["id"], row["name"], row["chips"])
        for key in PLAYER_FIELDS:
            setattr(player, key, row[key])
        game.players.append(player)
    if data["deck"] is not None:
        game.deck = Deck.__new__(Deck)
        game.deck.rng = random.Random()
        game.deck.cards = data["deck"]
    return game
