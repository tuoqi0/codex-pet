"""Regression checks against the actual seated asset and default-size render."""
import math
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PIL import Image, ImageChops
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QImage, QPainter, qAlpha
from PyQt5.QtWidgets import QApplication

import dragon_motion
from codex_pet import blink_overlay, gesture_overlay, wave_alternate, _GESTURE_REGIONS


ROOT = Path(__file__).resolve().parents[1]


def render(frame, scale=.63):
    """Use the same target dimensions and scaling as CodexPet.paintEvent."""
    image = QImage(round(480 * scale), round(300 * scale), QImage.Format_RGBA8888)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.scale(scale, scale)
    bounds = frame.size().scaled(470, 266, Qt.KeepAspectRatio)
    painter.drawPixmap(QRectF((480 - bounds.width()) / 2, (300 - bounds.height()) / 2,
                             bounds.width(), bounds.height()), frame, QRectF(frame.rect()))
    painter.end()
    bits = image.bits()
    bits.setsize(image.sizeInBytes())
    return Image.frombytes('RGBA', (image.width(), image.height()), bytes(bits))


def visible_bounds(image, box):
    x0, y0, x1, y1 = box
    coords = [(x, y) for y in range(y0, y1) for x in range(x0, x1)
              if image.getpixel((x, y))[3] > 160]
    if not coords:
        raise AssertionError('expected visible artwork in motion region')
    return (min(x for x, _ in coords), min(y for _, y in coords),
            max(x for x, _ in coords), max(y for _, y in coords))


class DragonMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.frames = dragon_motion.motion_frames(ROOT / 'assets/dragon-seated.png')
        cls.renders = [render(frame) for frame in cls.frames]

    def test_local_blink_keeps_other_pixels_at_three_sizes_and_every_phase(self):
        blinking = dragon_motion.motion_frames(blink_overlay(
            ROOT / 'assets/dragon-seated.png', ROOT / 'assets/dragon-seated-blink.png'))
        self.assertEqual(len(blinking), len(self.frames))
        for factor in (.63 * .75, .63, .63 * 1.25):
            relative = factor / .63
            left = math.floor(157 * relative) - 2
            top = math.floor(38 * relative) - 2
            right = math.ceil(186 * relative) + 2
            bottom = math.ceil(57 * relative) + 2
            for index, (idle, blink) in enumerate(zip(self.frames, blinking)):
                with self.subTest(scale=factor, frame=index):
                    before, after = render(idle, factor), render(blink, factor)
                    difference = ImageChops.difference(before, after)
                    changed = difference.convert('RGB').getbbox()
                    self.assertIsNotNone(changed, 'blink must visibly change the eyes')
                    self.assertGreaterEqual(changed[0], left)
                    self.assertGreaterEqual(changed[1], top)
                    self.assertLessEqual(changed[2], right)
                    self.assertLessEqual(changed[3], bottom)
                    self.assertIsNone(difference.getchannel('A').getbbox(), 'blink must not fade the figure')

    def test_pose_and_pil_inputs_use_identical_canvas(self):
        self.assertEqual(len(self.frames), 24)
        expected = self.frames[0].size()
        for name in ('dragon-seated-blink.png', 'dragon-seated-wave.png',
                     'dragon-seated-wave2.png', 'dragon-seated-tea.png',
                     'dragon-seated-happy.png', 'dragon-seated-work.png'):
            with self.subTest(name=name):
                with Image.open(ROOT / 'assets' / name) as source:
                    frames = dragon_motion.motion_frames(source)
                for frame in frames:
                    self.assertEqual(frame.size(), expected)
                    self.assert_frame_edges_transparent(frame)
                for area in ((159, 39, 185, 65), (106, 86, 130, 101),
                             (163, 65, 188, 115)):
                    baseline = render(frames[0]).crop(area).tobytes()
                    for frame in (frames[6], frames[12], frames[18]):
                        self.assertEqual(render(frame).crop(area).tobytes(), baseline)

    def test_gestures_change_only_the_figure_region_they_affect(self):
        base = ROOT / 'assets/dragon-seated.png'
        cases = {
            'wave': ('dragon-seated-wave.png', (119, 46, 175, 113)),
            'tea': ('dragon-seated-tea.png', (119, 46, 184, 113)),
            'happy': ('dragon-seated-happy.png', (153, 37, 188, 60)),
            'work': ('dragon-seated-work.png', (119, 46, 211, 142)),
        }
        for pose, (filename, (left, top, right, bottom)) in cases.items():
            images = dragon_motion.motion_frames(gesture_overlay(
                base, ROOT / 'assets' / filename, _GESTURE_REGIONS[pose]))
            for i in (0, 6, 12, 18):
                diff = ImageChops.difference(self.renders[i], render(images[i]))
                changed = diff.convert('RGB').getbbox()
                self.assertIsNotNone(changed, pose)
                self.assertGreaterEqual(changed[0], left, pose)
                self.assertGreaterEqual(changed[1], top, pose)
                self.assertLessEqual(changed[2], right, pose)
                self.assertLessEqual(changed[3], bottom, pose)

    def test_alternate_wave_preserves_the_same_base_figure(self):
        base = ROOT / 'assets/dragon-seated.png'
        alternate = wave_alternate(ROOT / 'assets/dragon-seated-wave.png',
                                   ROOT / 'assets/dragon-seated-wave2.png')
        images = dragon_motion.motion_frames(gesture_overlay(
            base, alternate, _GESTURE_REGIONS['wave']))
        for i in (0, 6, 12, 18):
            changed = ImageChops.difference(self.renders[i], render(images[i])).convert('RGB').getbbox()
            self.assertIsNotNone(changed)
            self.assertGreaterEqual(changed[0], 119)
            self.assertGreaterEqual(changed[1], 46)
            self.assertLessEqual(changed[2], 175)
            self.assertLessEqual(changed[3], 113)

    def test_wings_and_tail_move_visibly_at_default_size(self):
        phases = [self.renders[index] for index in (0, 6, 12, 18)]
        left = [visible_bounds(image, (83, 23, 136, 76)) for image in phases]
        right = [visible_bounds(image, (218, 23, 295, 92)) for image in phases]
        tail = [visible_bounds(image, (230, 120, 299, 181)) for image in phases]
        self.assertGreaterEqual(max(b[1] for b in left) - min(b[1] for b in left), 3)
        self.assertGreaterEqual(max(b[1] for b in right) - min(b[1] for b in right), 8)
        self.assertGreaterEqual(max(b[2] for b in tail) - min(b[2] for b in tail), 12)

    def test_wings_and_tail_have_distinct_motion_phases(self):
        # Test the grids used to render the wings rather than the obsolete
        # flattened-art field. Their separate delays must produce different
        # motion at the same frame, while each cycle returns to its origin.
        first_left = dragon_motion._wing_displacement(0, 0)
        first_right = dragon_motion._wing_displacement(0, 1)
        for bounds, points in first_left:
            x0, y0, x1, y1 = bounds
            self.assertEqual(points, (x0, y0, x0, y1, x1, y1, x1, y0))
        self.assertNotEqual(first_right, first_left)
        for side, initial in ((0, first_left), (1, first_right)):
            later = dragon_motion._wing_displacement(6, side)
            self.assertNotEqual(later, initial)
            cycle = dragon_motion._wing_displacement(dragon_motion._FRAME_COUNT, side)
            for (bounds, points), (cycle_bounds, cycle_points) in zip(initial, cycle):
                self.assertEqual(bounds, cycle_bounds)
                for value, cycle_value in zip(points, cycle_points):
                    self.assertAlmostEqual(value, cycle_value, places=10)
        w, h = dragon_motion._ART_SIZE
        tail = (dragon_motion._MARGIN_X + w * .95, dragon_motion._MARGIN_Y + h * .75)
        start = dragon_motion._tail_source_point(*tail, 0)
        self.assertNotEqual(start, tail)
        self.assertNotEqual(start, dragon_motion._tail_source_point(*tail, math.pi / 2))
        for value, cycle_value in zip(start, dragon_motion._tail_source_point(*tail, math.tau)):
            self.assertAlmostEqual(value, cycle_value, places=10)

    def test_motion_extents_remain_continuous_at_default_size(self):
        # Include the last-to-first transition: a seamless 24-frame cycle.
        for area, axis in (((83, 23, 136, 76), 1),
                           ((218, 23, 295, 92), 1),
                           ((230, 120, 299, 181), 2)):
            positions = [visible_bounds(image, area)[axis] for image in self.renders]
            for current, following in zip(positions, positions[1:] + positions[:1]):
                self.assertLessEqual(abs(following - current), 4)

    def test_face_and_knees_do_not_move_between_motion_frames(self):
        for area in ((159, 39, 185, 65), (106, 86, 130, 101), (163, 65, 188, 115)):
            baseline = self.renders[0].crop(area).tobytes()
            for index, frame in enumerate(self.renders[1:], 1):
                with self.subTest(area=area, frame=index):
                    self.assertEqual(frame.crop(area).tobytes(), baseline)

    def assert_frame_edges_transparent(self, frame):
        image = frame.toImage()
        w, h = image.width(), image.height()
        self.assertTrue(all(qAlpha(image.pixel(x, 0)) == 0 and qAlpha(image.pixel(x, h - 1)) == 0
                            for x in range(w)))
        self.assertTrue(all(qAlpha(image.pixel(0, y)) == 0 and qAlpha(image.pixel(w - 1, y)) == 0
                            for y in range(h)))

    def test_art_does_not_touch_default_window_edges(self):
        for index, image in enumerate(self.renders):
            with self.subTest(frame=index):
                self.assert_frame_edges_transparent(self.frames[index])
                left, top, right, bottom = image.getchannel('A').getbbox()
                self.assertGreater(left, 0)
                self.assertGreater(top, 0)
                self.assertLess(right, image.width)
                self.assertLess(bottom, image.height)

    def test_mesh_cells_do_not_fold(self):
        width = dragon_motion._ART_SIZE[0] + 2 * dragon_motion._MARGIN_X
        height = dragon_motion._ART_SIZE[1] + 2 * dragon_motion._MARGIN_Y
        xs = [round(width * i / dragon_motion._COLUMNS) for i in range(dragon_motion._COLUMNS + 1)]
        ys = [round(height * i / dragon_motion._ROWS) for i in range(dragon_motion._ROWS + 1)]

        def determinant(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

        def assert_quad(tl, tr, br, bl):
            for a, b, c in ((tl, tr, bl), (tr, br, tl), (br, bl, tr), (bl, tl, br)):
                self.assertGreater(determinant(a, b, c), 0)

        for index in range(dragon_motion._FRAME_COUNT):
            phase = 2 * math.pi * index / dragon_motion._FRAME_COUNT
            for side in (0, 1):
                for _, points in dragon_motion._wing_displacement(index, side):
                    tl, bl, br, tr = [tuple(points[i:i + 2]) for i in range(0, 8, 2)]
                    assert_quad(tl, tr, br, bl)
            for x0, x1 in zip(xs, xs[1:]):
                for y0, y1 in zip(ys, ys[1:]):
                    tl = dragon_motion._tail_source_point(x0, y0, phase)
                    tr = dragon_motion._tail_source_point(x1, y0, phase)
                    bl = dragon_motion._tail_source_point(x0, y1, phase)
                    br = dragon_motion._tail_source_point(x1, y1, phase)
                    assert_quad(tl, tr, br, bl)


if __name__ == '__main__':
    unittest.main()
