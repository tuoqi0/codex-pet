"""Exercise layout, visibility and lifecycle without a compositor."""
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from speech_bubble import SpeechBubble


class SpeechBubbleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.pet = QWidget(None, Qt.Tool)
        self.pet.resize(200, 120)
        self.pet.move(20, 20)
        self.pet.show()
        self.bubble = SpeechBubble()

    def tearDown(self):
        self.bubble.close()
        self.pet.close()
        self.app.processEvents()

    def test_chinese_text_wraps_and_stays_in_available_screen(self):
        text = '我陪你有一阵子了，要不要起身舒展一下？记得喝水，忙完也别忘了吃饭哦。'
        duration = self.bubble.show_message(text, self.pet)
        self.app.processEvents()
        area = self.pet.screen().availableGeometry()
        self.assertEqual(self.bubble.message.text(), text)
        self.assertTrue(area.contains(self.bubble.frameGeometry()))
        self.assertGreater(duration, SpeechBubble.MIN_DURATION_MS)
        self.assertGreater(self.bubble.message.height(), self.bubble.message.fontMetrics().height())
        self.assertEqual(self.bubble.scroll.verticalScrollBar().maximum(), 0)
        self.assertTrue(self.bubble.timer.isActive())
        self.bubble.dismiss.click()
        self.assertFalse(self.bubble.isVisible())
        self.assertFalse(self.bubble.timer.isActive())

    def test_very_long_text_remains_available_by_scrolling(self):
        text = '今天的天气变化很大，外出前可以确认天气。' * 50
        self.bubble.show_message(text, self.pet)
        self.app.processEvents()
        bar = self.bubble.scroll.verticalScrollBar()
        self.assertEqual(self.bubble.message.text(), text)
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        self.assertEqual(bar.value(), bar.maximum())
        self.assertGreaterEqual(self.bubble.message.height(), self.bubble.message.sizeHint().height())
        self.assertTrue(self.pet.screen().availableGeometry().contains(self.bubble.frameGeometry()))

    def test_timer_dismisses_and_new_message_restarts_timer(self):
        self.bubble.show_message('第一条', self.pet, duration_ms=30)
        self.bubble.show_message('第二条', self.pet, duration_ms=150)
        QTest.qWait(60)
        self.assertTrue(self.bubble.isVisible())
        self.assertEqual(self.bubble.message.text(), '第二条')
        QTest.qWait(150)
        self.assertFalse(self.bubble.isVisible())


if __name__ == '__main__':
    unittest.main()
