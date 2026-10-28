"""Exercise Qt mouse handling without requiring a desktop compositor."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt5.QtCore import QPoint, QEvent, Qt
from PyQt5.QtGui import QMouseEvent, qAlpha
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
import codex_pet


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = mock.Mock()
        self.pet = codex_pet.CodexPet(self.manager)
        self.pet.show()
        self.app.processEvents()

    def tearDown(self):
        self.pet.close()
        self.pet.deleteLater()
        self.app.processEvents()

    def test_single_click_greets_only_after_double_click_interval(self):
        QTest.mouseClick(self.pet, Qt.LeftButton, pos=QPoint(160, 100))
        self.assertEqual(self.pet.behavior.message, '')
        self.assertTrue(self.pet.click_timer.isActive())
        QTest.qWait(QApplication.doubleClickInterval() + 100)
        self.assertTrue(self.pet.behavior.message)
        self.assertEqual(self.pet.behavior.reaction_pose, 'happy')
        self.manager.open_floating.assert_not_called()

    def test_double_click_opens_panel_and_cancels_single_click(self):
        pos = QPoint(160, 100)
        QTest.mouseClick(self.pet, Qt.LeftButton, pos=pos)
        self.assertTrue(self.pet.click_timer.isActive())
        QTest.mouseDClick(self.pet, Qt.LeftButton, pos=pos)
        QTest.mouseRelease(self.pet, Qt.LeftButton, pos=pos)
        self.manager.open_floating.assert_called_once_with(self.pet)
        self.assertFalse(self.pet.click_timer.isActive())
        QTest.qWait(QApplication.doubleClickInterval() + 100)
        self.assertEqual(self.pet.behavior.message, '')

    def test_drag_does_not_greet_or_open_panel(self):
        original = self.pet.pos()
        pos = QPoint(160, 100)
        QTest.mousePress(self.pet, Qt.LeftButton, pos=pos)
        moved = QPoint(200, 130)
        event = QMouseEvent(QEvent.MouseMove, moved, self.pet.mapToGlobal(moved),
                            Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(self.pet, event)
        self.assertTrue(self.pet.drag_moved)
        self.assertTrue(self.pet.behavior.dragging)
        if not self.pet.wayland:
            self.assertEqual(self.pet.pos() - original, QPoint(40, 30))
        QTest.mouseRelease(self.pet, Qt.LeftButton, pos=moved)
        self.assertFalse(self.pet.behavior.dragging)
        self.assertIsNotNone(self.pet.behavior.landed_at)
        QTest.qWait(QApplication.doubleClickInterval() + 100)
        self.assertEqual(self.pet.behavior.message, '')
        self.manager.open_floating.assert_not_called()

    def test_drag_reacts_to_pointer_and_returns_to_rest_without_changing_pose(self):
        self.pet.behavior.set_activity('work', 0)
        origin = QPoint(160, 100)
        QTest.mousePress(self.pet, Qt.LeftButton, pos=origin)
        moved = QPoint(200, 100)
        event = QMouseEvent(QEvent.MouseMove, moved, self.pet.mapToGlobal(moved),
                            Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(self.pet, event)
        QTest.qWait(100)
        self.assertEqual(self.pet.behavior.pose(999999999), 'work')
        self.assertLess(self.pet.drag_motion.angle, -1)
        self.assertGreater(self.pet.drag_motion.lift, 1)
        QTest.mouseRelease(self.pet, Qt.LeftButton, pos=moved)
        QTest.qWait(1000)
        self.assertLess(abs(self.pet.drag_motion.angle), .1)
        self.assertLess(self.pet.drag_motion.lift, .1)

    def test_drag_extreme_tilts_do_not_clip_artwork(self):
        # Hold the animation clock fixed.  The tail reaches the bottom edge in
        # phases 21–23 at +9 degrees in the uncorrected implementation; random
        # clock sampling misses that fault most of the time.
        self.pet.timer.stop()
        self.pet.drag_motion.lift = self.pet.drag_motion.LIFT
        for scale in (.63 * .75, .63, .63 * 1.25):
            self.pet.set_scale(scale)
            for pose in self.pet.frames:
                with mock.patch.object(self.pet.behavior, 'pose', return_value=pose):
                    for angle in (-self.pet.drag_motion.MAX_ANGLE, self.pet.drag_motion.MAX_ANGLE):
                        self.pet.drag_motion.angle = angle
                        for phase in range(24):
                            # Cover different bob positions as well as the 24
                            # deformation frames; the cycles are asynchronous.
                            for cycle in range(4):
                                now = 1000 + phase * .1 + cycle * 2.4
                                with self.subTest(scale=scale, pose=pose, angle=angle,
                                                  phase=phase, cycle=cycle), \
                                     mock.patch('codex_pet.time.monotonic', return_value=now):
                                    image = self.pet.grab().toImage()
                                    width, height = image.width(), image.height()
                                    edges = ([(x, 0) for x in range(width)] +
                                             [(x, height - 1) for x in range(width)] +
                                             [(0, y) for y in range(height)] +
                                             [(width - 1, y) for y in range(height)])
                                    self.assertTrue(all(qAlpha(image.pixel(x, y)) == 0 for x, y in edges))

    def test_drag_cancels_a_pending_single_click(self):
        pos = QPoint(160, 100)
        QTest.mouseClick(self.pet, Qt.LeftButton, pos=pos)
        self.assertTrue(self.pet.click_timer.isActive())
        QTest.mousePress(self.pet, Qt.LeftButton, pos=pos)
        moved = QPoint(200, 130)
        event = QMouseEvent(QEvent.MouseMove, moved, self.pet.mapToGlobal(moved),
                            Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(self.pet, event)
        QTest.mouseRelease(self.pet, Qt.LeftButton, pos=moved)
        QTest.qWait(QApplication.doubleClickInterval() + 100)
        self.assertEqual(self.pet.behavior.message, '')
        self.manager.open_floating.assert_not_called()

    def test_seated_wings_and_tail_move_without_fading_centre(self):
        # The seated character fills this wide image differently from the old
        # standing image: both wings are near the upper sides, tail lower right.
        frames = [frame.toImage() for frame in self.pet.frames['idle']]
        first, quarter = frames[0], frames[6]
        w, h = first.width(), first.height()

        def differences(x0, y0, x1, y1):
            return sum(first.pixel(x, y) != quarter.pixel(x, y)
                       for x in range(int(w*x0), int(w*x1), 3)
                       for y in range(int(h*y0), int(h*y1), 3))

        self.assertGreater(differences(.25, .22, .47, .50), 20)
        self.assertGreater(differences(.73, .22, .92, .50), 20)
        self.assertGreater(differences(.72, .53, .92, .84), 20)
        # The old .40–.60 full-height rectangle included the left wing tip.
        # Check the face, body and knees independently, leaving that wing free
        # to move. Default-window pixel stability is also checked separately in
        # test_dragon_motion against every motion phase.
        self.assertEqual(differences(.53, .14, .64, .30), 0)
        self.assertEqual(differences(.52, .30, .65, .65), 0)
        self.assertEqual(differences(.35, .45, .43, .52), 0)
        # A fully drawn centre remains opaque across every motion frame: no
        # global alpha fade or overlay that would make the face flicker.
        for frame in frames:
            opaque = sum(qAlpha(frame.pixel(x, y)) >= 230
                         for x in range(int(w*.50), int(w*.63), 2)
                         for y in range(int(h*.17), int(h*.27), 2))
            sample_count = (len(range(int(w*.50), int(w*.63), 2))
                            * len(range(int(h*.17), int(h*.27), 2)))
            self.assertGreater(opaque, sample_count * .75)
            self.assertEqual(opaque, sum(qAlpha(first.pixel(x, y)) >= 230
                                         for x in range(int(w*.50), int(w*.63), 2)
                                         for y in range(int(h*.17), int(h*.27), 2)))

        # Check the actual widget rendering as well as source frames; every
        # pose must keep the face area opaque when it is painted.
        for pose in ('idle', 'blink', 'wave', 'happy', 'work'):
            with mock.patch.object(self.pet.behavior, 'pose', return_value=pose), \
                    mock.patch('codex_pet.time.monotonic', return_value=100):
                image = self.pet.grab().toImage()
            iw, ih = image.width(), image.height()
            alphas = [qAlpha(image.pixel(x, y))
                      for x in range(int(iw*.50), int(iw*.63))
                      for y in range(int(ih*.17), int(ih*.27))]
            self.assertGreater(sum(alpha >= 230 for alpha in alphas), len(alphas) * .75)

    def test_wave_alternates_hand_at_default_size_without_changing_face_or_outline(self):
        self.assertEqual((self.pet.width(), self.pet.height()), (302, 189))
        renders = []
        for pose in ('wave', 'wave2'):
            with mock.patch.object(self.pet.behavior, 'pose', return_value=pose), \
                    mock.patch('codex_pet.time.monotonic', return_value=100):
                renders.append(self.pet.grab().toImage())
        first, second = renders
        w, h = first.width(), first.height()
        hand_box = (int(w * .43), int(h * .20), int(w * .56), int(h * .45))
        changed = []
        visible_bounds = []
        for frame in renders:
            visible = [(x, y) for y in range(h) for x in range(w) if qAlpha(frame.pixel(x, y))]
            visible_bounds.append((min(x for x, _ in visible), min(y for _, y in visible),
                                   max(x for x, _ in visible), max(y for _, y in visible)))
        self.assertEqual(visible_bounds[0], visible_bounds[1])
        for y in range(h):
            for x in range(w):
                if first.pixel(x, y) != second.pixel(x, y):
                    changed.append((x, y))
        self.assertGreater(len(changed), 100)
        self.assertTrue(all(hand_box[0] <= x < hand_box[2] and hand_box[1] <= y < hand_box[3]
                            for x, y in changed))


if __name__ == '__main__':
    unittest.main()
