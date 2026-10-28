"""Input shape unit tests and optional isolated-X11 interaction test.

Run the X11 test on a disposable server with PET_TEST_X11=1 and
QT_QPA_PLATFORM=xcb. It synthesizes its own Q-style artwork.
"""
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PIL import Image, ImageDraw
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QColor, QImage, QPainter
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from input_shape import InputShape, _Rect


class ShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_offscreen_platform_falls_back_without_native_display(self):
        with mock.patch.object(QApplication, 'platformName', return_value='offscreen'):
            shape = InputShape()
        self.assertIsNone(shape.display)
        widget = mock.Mock()
        image = QImage(10, 10, QImage.Format_RGBA8888)
        shape.set(widget, image)
        widget.winId.assert_not_called()
        shape.close()

    def test_missing_xshape_library_falls_back(self):
        with mock.patch.object(QApplication, 'platformName', return_value='xcb'), \
                mock.patch('input_shape.ctypes.util.find_library', return_value='libX11.so.6'), \
                mock.patch('input_shape.ctypes.CDLL', side_effect=OSError('not installed')):
            shape = InputShape()
        self.assertIsNone(shape.display)
        widget = mock.Mock()
        shape.set(widget, QImage(10, 10, QImage.Format_RGBA8888))
        widget.winId.assert_not_called()

    def test_no_shape_extension_closes_display(self):
        x11, extension = mock.MagicMock(), mock.MagicMock()
        x11.XOpenDisplay.return_value = 123
        extension.XShapeQueryExtension.return_value = 0
        with mock.patch.object(QApplication, 'platformName', return_value='xcb'), \
                mock.patch('input_shape.ctypes.util.find_library', side_effect=['x11', 'xext']), \
                mock.patch('input_shape.ctypes.CDLL', side_effect=[x11, extension]):
            shape = InputShape()
        self.assertIsNone(shape.display)
        x11.XCloseDisplay.assert_called_once_with(123)

    def test_rectangles_use_physical_pixels_at_high_dpi(self):
        # Capture requests at the X11 boundary. The region should contain the
        # scaled figure and exclude the far corner at either device scale.
        for ratio in (1, 2):
            shape = InputShape.__new__(InputShape)
            shape.display = 1
            rects = []
            shape.x11 = mock.Mock()
            shape.ext = mock.Mock()
            def collect(display, window, kind, x, y, array, count, op, ordering):
                self.assertEqual(kind, 2)  # ShapeInput; not bounding/painting
                rects.extend((array[i].x, array[i].y, array[i].width, array[i].height)
                             for i in range(count))
            shape.ext.XShapeCombineRectangles.side_effect = collect
            widget = mock.Mock()
            widget.devicePixelRatioF.return_value = ratio
            widget.winId.return_value = 42
            image = QImage(100*ratio, 100*ratio, QImage.Format_RGBA8888)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.fillRect(40*ratio, 40*ratio, 20*ratio, 20*ratio, QColor('red'))
            painter.end()
            shape.set(widget, image, margin=4)
            def covers(x, y):
                return any(a <= x < a+w and b <= y < b+h for a,b,w,h in rects)
            self.assertTrue(covers(50*ratio, 50*ratio))
            self.assertFalse(covers(0, 0))
            self.assertTrue(all(a+w <= 100*ratio and b+h <= 100*ratio for a,b,w,h in rects))


@unittest.skipUnless(os.environ.get('PET_TEST_X11') == '1', 'requires an isolated X11 server')
class X11ShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if QApplication.platformName() != 'xcb':
            raise unittest.SkipTest('xcb platform required')
        cls.x = ctypes.CDLL('libX11.so.6')
        cls.xtest = ctypes.CDLL('libXtst.so.6')
        cls.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        cls.x.XOpenDisplay.restype = ctypes.c_void_p
        cls.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cls.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        cls.xtest.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        cls.xtest.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        cls.display = cls.x.XOpenDisplay(None)
        if not cls.display:
            raise unittest.SkipTest('no isolated X11 display')

    @classmethod
    def tearDownClass(cls):
        cls.x.XCloseDisplay(cls.display)

    def tick(self):
        self.x.XSync(self.display, 0)
        self.app.processEvents()

    def move(self, x, y):
        # XTest coordinates are physical pixels; Qt positions are logical.
        ratio = self.app.primaryScreen().devicePixelRatio()
        self.xtest.XTestFakeMotionEvent(self.display, -1, round(x*ratio), round(y*ratio), 0)
        self.tick()

    def button(self, down):
        self.xtest.XTestFakeButtonEvent(self.display, 1, down, 0)
        self.tick()

    def assert_bounding_unchanged(self, pet):
        # Input shape must never trim the area the compositor can draw.
        extension = pet.input_shape.ext
        extension.XShapeGetRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                                 ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        extension.XShapeGetRectangles.restype = ctypes.POINTER(_Rect)
        pet.input_shape.x11.XFree.argtypes = [ctypes.c_void_p]
        self.tick()
        count, order = ctypes.c_int(), ctypes.c_int()
        rectangles = extension.XShapeGetRectangles(pet.input_shape.display, int(pet.winId()), 0,
                                                    ctypes.byref(count), ctypes.byref(order))
        try:
            self.assertEqual(count.value, 1)
            rect = rectangles[0]
            ratio = pet.devicePixelRatioF()
            self.assertEqual((rect.x, rect.y, rect.width, rect.height),
                             (0, 0, round(pet.width()*ratio), round(pet.height()*ratio)))
        finally:
            pet.input_shape.x11.XFree(rectangles)

    def test_clicks_drag_scale_animation_and_close(self):
        from codex_pet import QPet
        class Underlay(QWidget):
            def __init__(self):
                super().__init__(); self.clicks = 0
            def mousePressEvent(self, event):
                self.clicks += 1
        class Manager:
            def window_moved(self, pet): pass
            def on_user_greet(self, pet): pass
            def open_floating(self, pet): pass
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, box in [('idle', (25,25,75,75)), ('wave', (5,25,55,75))]:
                image = Image.new('RGBA', (100,100))
                ImageDraw.Draw(image).ellipse(box, fill='white')
                image.save(root / (name + '.webp'))
            (root / '素材清单.json').write_text(json.dumps({
                'idle': {'id':'idle','file':'idle.webp'},
                'variants':[{'id':'01','file':'wave.webp'}],
                'dragAnimation':{'id':'drag','file':'idle.webp'},
                'landingAnimation':{'id':'landing','file':'idle.webp'},
            }), encoding='utf-8')
            under = Underlay(); under.setGeometry(0, 0, 500, 500); under.show()
            pet = QPet(Manager(), root); pet.move(80,80); pet.show(); pet.player.pause(); QTest.qWait(40); self.tick()
            self.assertTrue(pet.input_shape.display, 'XShape >= 1.1 required')
            self.assert_bounding_unchanged(pet)
            def click(point):
                pos = pet.mapToGlobal(point); self.move(pos.x(), pos.y()); self.button(1); self.button(0)
            def drag(point):
                before = pet.pos(); pos = pet.mapToGlobal(point)
                self.move(pos.x(), pos.y()); self.button(1); self.move(pos.x()+30, pos.y()+20); self.button(0)
                self.assertEqual(pet.pos() - before, QPoint(30,20))
            click(QPoint(1,1)); self.assertEqual(under.clicks,1)
            drag(QPoint(90,90))
            pet.player.play('idle'); pet.set_scale(.8); self.tick()
            click(QPoint(1,1)); self.assertEqual(under.clicks,2)
            drag(QPoint(72,72))
            pet.player.play('01'); self.tick()
            with mock.patch.object(pet.input_shape, 'set', wraps=pet.input_shape.set) as shaped:
                pet.windowHandle().screenChanged.emit(pet.screen())
                self.assertEqual(shaped.call_count, 1)
            click(QPoint(1,1)); self.assertEqual(under.clicks,3)
            # One-shot changed frame still supports dragging on its visible area.
            drag(QPoint(45,72))
            pet.close(); self.tick()
            self.assertIsNone(pet.input_shape.display)
            under.close()

    def test_dragon_shape_and_drag(self):
        from codex_pet import CodexPet
        class Underlay(QWidget):
            def __init__(self):
                super().__init__(); self.clicks = 0
            def mousePressEvent(self, event): self.clicks += 1
        under = Underlay(); under.setGeometry(0, 0, 700, 600); under.show()
        pet = CodexPet(); pet.move(80,80); pet.show(); QTest.qWait(40); self.tick()
        self.assertTrue(pet.input_shape.display)
        self.assert_bounding_unchanged(pet)
        pos = pet.mapToGlobal(QPoint(1,1)); self.move(pos.x(), pos.y()); self.button(1); self.button(0)
        self.assertEqual(under.clicks,1)
        before = pet.pos(); pos = pet.mapToGlobal(QPoint(160,100))
        self.move(pos.x(), pos.y()); self.button(1); self.move(pos.x()+30, pos.y()+20); self.button(0)
        self.assertEqual(pet.pos()-before, QPoint(30,20))
        pet.set_scale(.8); self.tick()
        pos = pet.mapToGlobal(QPoint(1,1)); self.move(pos.x(), pos.y()); self.button(1); self.button(0)
        self.assertEqual(under.clicks,2)
        with mock.patch.object(pet.input_shape, 'set', wraps=pet.input_shape.set) as shaped:
            pet.windowHandle().screenChanged.emit(pet.screen())
            self.assertEqual(shaped.call_count,1)
        pet.close(); self.tick(); self.assertIsNone(pet.input_shape.display)
        under.close()

if __name__ == '__main__':
    unittest.main()
