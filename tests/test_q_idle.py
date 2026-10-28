import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from q_idle import QIdleDirector, QWorkDirector


class PredictableRandom:
    def uniform(self, low, high):
        return low

    def choice(self, choices):
        return choices[0]


class Player:
    def __init__(self, entries=None):
        self.entries = dict.fromkeys(QIdleDirector.CANDIDATES if entries is None else entries)
        self.current = 'idle'
        self.played = []
        self.success = True

    def play(self, ident):
        self.played.append(ident)
        if self.success:
            self.current = ident
        return self.success


class IdleTests(unittest.TestCase):
    def setUp(self):
        self.director = QIdleDirector(0, PredictableRandom())
        self.player = Player()

    def test_waits_then_plays_different_catalogue_actions(self):
        self.assertIsNone(self.director.tick(0, True, self.player))
        self.assertIsNone(self.director.tick(24, True, self.player))
        self.assertEqual(self.director.tick(25, True, self.player), 'idle2')
        self.player.current = 'idle'
        self.assertIsNone(self.director.tick(26, True, self.player))
        self.assertEqual(self.director.tick(70, True, self.player), '01')

    def test_busy_hidden_and_interactions_never_preempt(self):
        self.director.tick(0, True, self.player)
        self.assertIsNone(self.director.tick(30, False, self.player))
        self.assertEqual(self.player.played, [])
        self.player.current = 'work02'
        self.assertIsNone(self.director.tick(35, True, self.player))
        self.player.current = 'idle'
        self.assertIsNone(self.director.tick(36, True, self.player))
        self.assertIsNone(self.director.tick(45, True, self.player))
        self.assertEqual(self.director.tick(46, True, self.player), 'idle2')

    def test_optional_missing_assets_and_failed_decode(self):
        self.player.entries = {'04': {}}
        self.director.tick(0, True, self.player)
        self.player.success = False
        self.assertIsNone(self.director.tick(25, True, self.player))
        self.assertEqual(self.player.played, ['04'])
        self.assertIsNone(self.director.tick(26, True, self.player))
        self.player.success = True
        self.assertEqual(self.director.tick(33, True, self.player), '04')
        self.player.current = 'idle'
        self.player.entries = {}
        self.director.tick(34, True, self.player)
        self.assertIsNone(self.director.tick(85, True, self.player))
        self.assertEqual(self.player.played, ['04', '04'])

    def test_backward_clock_restarts_idle_delay(self):
        self.director.tick(0, True, self.player)
        self.director.tick(100, False, self.player)
        self.assertIsNone(self.director.tick(10, True, self.player))
        self.assertIsNone(self.director.tick(34, True, self.player))
        self.assertEqual(self.director.tick(35, True, self.player), 'idle2')


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.director = QWorkDirector(PredictableRandom())
        self.player = Player(('work03', 'work04', 'work05'))
        self.player.current = 'work02'
        self.director.start(0)

    def test_waits_then_rotates_without_preempting_gestures(self):
        self.assertIsNone(self.director.tick(29, True, self.player))
        self.assertIsNone(self.director.tick(30, False, self.player))
        self.player.current = 'drag'
        self.assertIsNone(self.director.tick(31, True, self.player))
        self.player.current = 'work02'
        self.assertEqual(self.director.tick(32, True, self.player), 'work03')
        self.player.current = 'work02'
        self.assertEqual(self.director.tick(62, True, self.player), 'work04')
        self.director.stop()
        self.player.current = 'work02'
        self.assertIsNone(self.director.tick(100, True, self.player))

    def test_missing_or_failing_optional_asset_retries_without_falsifying_state(self):
        self.player.entries = {}
        self.assertIsNone(self.director.tick(30, True, self.player))
        self.player.entries = {'work03': {}}
        self.player.success = False
        self.assertIsNone(self.director.tick(60, True, self.player))
        self.player.success = True
        self.assertIsNone(self.director.tick(67, True, self.player))
        self.assertEqual(self.director.tick(68, True, self.player), 'work03')


if __name__ == '__main__':
    unittest.main()
