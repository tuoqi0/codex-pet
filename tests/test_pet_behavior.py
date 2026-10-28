import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from pet_behavior import DragMotion, PetBehavior


class PredictableRandom:
    def uniform(self, low, high):
        return low

    def choice(self, seq):
        return seq[-1]


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.pet = PetBehavior(0.0, rng=PredictableRandom())

    def test_blink_and_reaction_return_to_idle(self):
        self.pet.tick(3.0)
        self.assertEqual(self.pet.pose(3.0), 'blink')
        self.assertEqual(self.pet.pose(3.2), 'idle')
        self.pet.react(4.0, 'hello')
        self.assertEqual(self.pet.pose(4.1), 'wave')
        self.assertEqual(self.pet.pose(4.3), 'wave2')
        self.assertEqual(self.pet.pose(4.6), 'wave')
        self.assertEqual(self.pet.message, 'hello')
        self.assertEqual(self.pet.pose(6.9), 'idle')

    def test_blink_does_not_replace_an_active_idle_action(self):
        for pose in ('tea', 'wave', 'happy'):
            with self.subTest(pose=pose):
                self.pet.idle_pose = pose
                self.pet.idle_pose_until = 5.0
                self.pet.blink_until = 4.18
                self.assertEqual(self.pet.pose(4.0), pose)
                self.assertEqual(self.pet.pose(5.1), 'idle')

    def test_drink_reminder_uses_tea_pose_then_returns_to_idle(self):
        self.pet.react(4.0, '喝水', 'tea')
        self.assertEqual(self.pet.pose(4.5), 'tea')
        self.assertEqual(self.pet.pose(7.0), 'idle')

    def test_patrol_waits_then_moves_and_bounces(self):
        self.assertEqual(self.pet.tick(7.9), 0.0)
        self.assertGreater(self.pet.tick(8.0), 0.0)
        self.pet.at_edge()
        self.assertLess(self.pet.tick(8.05), 0.0)
        self.assertEqual(self.pet.tick(10.1), 0.0)

    def test_drag_and_reaction_pause_patrol(self):
        self.pet.tick(8.0)
        self.pet.begin_drag(1)
        self.assertEqual(self.pet.tick(8.1), 0.0)
        self.pet.end_drag(9.0)
        self.assertEqual(self.pet.tick(10.0), 0.0)
        self.pet.react(16.0, 'hi')
        self.assertEqual(self.pet.tick(16.1), 0.0)

    def test_disabled_patrol_and_bounded_timestep(self):
        self.pet.set_patrol(False, 0.0)
        self.assertEqual(self.pet.tick(100.0), 0.0)
        self.pet.set_patrol(True, 100.0)
        self.assertLessEqual(abs(self.pet.tick(200.0)), self.pet.PATROL_SPEED * .1)

    def test_drag_landing_and_work_drive_dragon_pose(self):
        self.pet.begin_drag(1)
        self.assertEqual(self.pet.pose(1), 'idle')
        self.pet.end_drag(2)
        self.assertEqual(self.pet.landed_at, 2)
        self.pet.set_activity('work', 3)
        self.assertEqual(self.pet.pose(3), 'work')
        self.pet.set_activity('idle', 4)
        self.assertEqual(self.pet.pose(4), 'idle')
        self.pet.set_activity('celebrate', 5)
        self.assertEqual(self.pet.pose(5), 'happy')
        self.pet.tick(5)
        self.assertEqual(self.pet.pose(5), 'happy')
        self.pet.set_activity('work', 5.5)
        self.pet.set_activity('idle', 5.8)
        self.assertEqual(self.pet.pose(5.8), 'idle')


class DragMotionTests(unittest.TestCase):
    def test_direction_follows_pointer_and_returns_to_rest(self):
        motion = DragMotion()
        motion.begin(0.0, 100)
        motion.move(.05, 130)
        motion.advance(.10)
        self.assertLess(motion.angle, -1)
        self.assertGreater(motion.lift, 0)
        motion.move(.15, 80)
        for i in range(5):
            motion.advance(.17 + i * .02)
        self.assertGreater(motion.angle, 0)
        motion.end(.3)
        for i in range(1, 101):
            motion.advance(.3 + i * .02)
        self.assertAlmostEqual(motion.angle, 0, delta=.01)
        self.assertAlmostEqual(motion.lift, 0, delta=.01)

    def test_pose_stays_the_same_when_dragging_a_working_pet(self):
        pet = PetBehavior(0, rng=PredictableRandom())
        pet.set_activity('work', 1)
        pet.begin_drag(1.1)
        pet.set_activity('idle', 1.2)
        self.assertEqual(pet.pose(1.2), 'work')
        pet.end_drag(1.3)
        self.assertEqual(pet.pose(1.3), 'idle')

if __name__ == '__main__':
    unittest.main()
