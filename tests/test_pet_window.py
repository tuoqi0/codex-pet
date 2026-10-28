"""Exercise actual Qt mouse handling without requiring a desktop compositor."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt5.QtCore import QPoint, QEvent, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
import codex_pet


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Only event behavior is under test here, so load the existing image for
        # all poses. Package rendering is checked separately with real assets.
        image = Path(__file__).resolve().parents[1] / 'assets' / 'dragon-lady.png'
        with mock.patch.object(codex_pet, 'asset_path', return_value=image):
            self.pet = codex_pet.CodexPet()
        self.pet.show()
        self.app.processEvents()

    def tearDown(self):
        self.pet.close()
        self.pet.deleteLater()
        self.app.processEvents()

    def test_click_and_double_click_greet(self):
        QTest.mouseClick(self.pet, Qt.LeftButton, pos=QPoint(160, 200))
        self.assertTrue(self.pet.behavior.message)
        self.assertEqual(self.pet.behavior.pose(codex_pet.time.monotonic()), 'wave')
        QTest.mouseDClick(self.pet, Qt.LeftButton, pos=QPoint(160, 200))
        self.assertEqual(self.pet.behavior.message, '很高興見到你！')

    def test_drag_does_not_greet(self):
        pos = QPoint(160, 200)
        QTest.mousePress(self.pet, Qt.LeftButton, pos=pos)
        moved = QPoint(200, 230)
        event = QMouseEvent(QEvent.MouseMove, moved, self.pet.mapToGlobal(moved),
                            Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(self.pet, event)
        self.assertTrue(self.pet.drag_moved)
        QTest.mouseRelease(self.pet, Qt.LeftButton, pos=moved)
        self.assertFalse(self.pet.behavior.dragging)
        self.assertEqual(self.pet.behavior.message, '')


if __name__ == '__main__':
    unittest.main()
