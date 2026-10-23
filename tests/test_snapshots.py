"""Migration foundation: restart during play, then finish with identical payouts."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from poker.game import Game
from poker.snapshots import SnapshotError, snapshot_game, restore_game


def restart(game):
    # Real storage boundary, rather than passing the same Python object back.
    return restore_game(json.loads(json.dumps(snapshot_game(game))))


def advance(game, aggressive=False):
    if game.awaiting_runout:
        game.deal_next_runout()
    else:
        player = game.players[game.to_act]
        legal = game.legal_actions(player.id)
        if aggressive and legal["can_raise"]:
            action, amount = "raise", legal["max_raise_to"]
        else:
            action, amount = ("check" if legal["can_check"] else "call"), 0
        error = game.act(player.id, action, amount)
        if error:
            raise AssertionError(error)


class SnapshotTests(unittest.TestCase):
    def assert_same(self, original, restored):
        self.assertEqual(snapshot_game(original), snapshot_game(restored))
        self.assertEqual(original.public_state(), restored.public_state())
        for player in original.players:
            self.assertEqual(original.private_state(player.id), restored.private_state(player.id))

    def test_restart_every_action_and_runout_all_variants(self):
        for variant in ("holdem", "omaha", "omaha2"):
            for betting in ("nl", "pl"):
                for aggressive in (False, True):
                    with self.subTest(variant=variant, betting=betting, aggressive=aggressive):
                        game = Game()
                        game.set_variant(variant, betting)
                        # Unequal stacks exercise main/side pots and all-in runouts.
                        for i, chips in enumerate((75, 170, 450, 1000)):
                            game.add_player(str(i), f"Player{i}", chips)
                        self.assertTrue(game.start_hand())
                        total = sum(p.chips + p.committed for p in game.players)
                        for step in range(100):
                            restored = restart(game)
                            self.assert_same(game, restored)
                            if not game.hand_in_progress:
                                break
                            advance(game, aggressive)
                            advance(restored, aggressive)
                            self.assert_same(game, restored)
                            game = restored
                        else:
                            self.fail("Hand did not finish")
                        self.assertEqual(sum(p.chips for p in game.players), total)
                        self.assertEqual(game.hand_count, 1)

    def test_empty_table_and_between_hands(self):
        game = Game()
        self.assert_same(game, restart(game))
        game.add_player("a", "A", 1000)
        game.add_player("b", "B", 1000)
        game.start_hand()
        game.act(game.players[game.to_act].id, "fold")
        restored = restart(game)
        self.assert_same(game, restored)
        self.assertEqual(game.replay_list(), restored.replay_list())
        self.assertTrue(restored.start_hand())

    def test_live_blinds_sitting_out_and_pending_departure(self):
        game = Game()
        for i in range(4):
            game.add_player(str(i), str(i), 1000)
        game.set_sitting_out("0", True)
        game.start_hand()
        game.set_blinds(20, 40)
        game.remove_player("1")
        self.assertTrue(game.players[1].pending_removal)
        restored = restart(game)
        self.assert_same(game, restored)
        self.assertEqual(restored.hand_bb, 10)
        while restored.hand_in_progress:
            advance(restored)
        self.assertEqual([p.id for p in restored.players], ["0", "2", "3"])

    def test_snapshot_and_restored_state_do_not_alias(self):
        game = Game()
        game.add_player("a", "A", 1000)
        game.add_player("b", "B", 1000)
        game.start_hand()
        data = snapshot_game(game)
        restored = restore_game(data)
        data["players"][0]["hole"].clear()
        self.assertTrue(game.players[0].hole)
        self.assertTrue(restored.players[0].hole)
        restored.deck.deal_one()
        self.assertNotEqual(game.deck.cards, restored.deck.cards)

    def test_refuses_corruption_and_unsupported_schemas(self):
        game = Game()
        game.add_player("a", "A", 1000)
        game.add_player("b", "B", 1000)
        game.start_hand()
        valid = snapshot_game(game)
        bad = []
        for version in (None, True, 0, 2):
            row = copy.deepcopy(valid)
            row["version"] = version
            bad.append(row)
        for key, value in (("pot", -1), ("bb", True), ("to_act", 9), ("phase", "unknown")):
            row = copy.deepcopy(valid)
            row["game"][key] = value
            bad.append(row)
        row = copy.deepcopy(valid)
        row["deck"][0] = row["players"][0]["hole"][0]
        bad.append(row)
        row = copy.deepcopy(valid)
        row["players"][1]["id"] = "a"
        bad.append(row)
        row = copy.deepcopy(valid)
        del row["game"]["min_raise"]
        bad.append(row)
        for row in bad:
            with self.subTest(row=row):
                with self.assertRaises(SnapshotError):
                    restore_game(row)
        game.unmapped_future_setting = True
        with self.assertRaises(SnapshotError):
            snapshot_game(game)


if __name__ == "__main__":
    unittest.main()
