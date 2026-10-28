"""Regression checks against the actual seated asset and default-size render."""
import math
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PIL import Image
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QImage, QPainter, qAlpha
from PyQt5.QtWidgets import QApplication

import dragon_motion


ROOT = Path(__file__).resolve().parents[1]


def default_render(frame):
    """Use the same target dimensions and scaling as CodexPet.paintEvent."""
    image = QImage(302, 189, QImage.Format_RGBA8888)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.scale(.63, .63)
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
        cls.renders = [default_render(frame) for frame in cls.frames]

    def test_pose_and_pil_inputs_use_identical_canvas(self):
        self.assertEqual(len(self.frames), 12)
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

    def test_wings_and_tail_move_visibly_at_default_size(self):
        phases = [self.renders[index] for index in (0, 3, 6, 9)]
        left = [visible_bounds(image, (83, 23, 136, 76)) for image in phases]
        right = [visible_bounds(image, (218, 23, 295, 92)) for image in phases]
        tail = [visible_bounds(image, (230, 120, 299, 181)) for image in phases]
        self.assertGreaterEqual(max(b[1] for b in left) - min(b[1] for b in left), 3)
        self.assertGreaterEqual(max(b[1] for b in right) - min(b[1] for b in right), 4)
        self.assertGreaterEqual(max(b[2] for b in tail) - min(b[2] for b in tail), 5)

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

        for index in range(12):
            phase = 2 * math.pi * index / 12
            for x0, x1 in zip(xs, xs[1:]):
                for y0, y1 in zip(ys, ys[1:]):
                    tl = dragon_motion._source_point(x0, y0, phase)
                    tr = dragon_motion._source_point(x1, y0, phase)
                    bl = dragon_motion._source_point(x0, y1, phase)
                    br = dragon_motion._source_point(x1, y1, phase)
                    self.assertGreater(determinant(tl, tr, bl), 0)
                    self.assertGreater(determinant(tr, br, tl), 0)
                    self.assertGreater(determinant(br, bl, tr), 0)
                    self.assertGreater(determinant(bl, tl, br), 0)


if __name__ == '__main__':
    unittest.main()
