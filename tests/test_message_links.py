import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QMouseEvent, QTextCursor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from pet_panel import MessageBubble


class MessageLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_click_opens_only_web_links_without_breaking_selection(self):
        bubble = MessageBubble('[documentation](https://example.com/docs) [local](file:///tmp/test)', 'assistant')
        bubble.resize(500, 100)
        bubble.show()
        QTest.qWait(30)
        cursor = QTextCursor(bubble.document())
        cursor.setPosition(3)
        web_position = bubble.cursorRect(cursor).center()
        cursor.setPosition(len('documentation ') + 2)
        local_position = bubble.cursorRect(cursor).center()
        self.assertEqual(bubble.anchorAt(web_position), 'https://example.com/docs')
        self.assertEqual(bubble.anchorAt(local_position), 'file:///tmp/test')
        with mock.patch('pet_panel.QDesktopServices.openUrl') as open_url:
            QTest.mouseClick(bubble.viewport(), Qt.LeftButton, pos=web_position)
            self.assertEqual(open_url.call_count, 1)
            self.assertEqual(open_url.call_args.args[0].toString(), 'https://example.com/docs')
            QTest.mouseClick(bubble.viewport(), Qt.LeftButton, pos=local_position)
            self.assertEqual(open_url.call_count, 1)
            QTest.mousePress(bubble.viewport(), Qt.LeftButton, pos=web_position)
            QApplication.sendEvent(bubble.viewport(), QMouseEvent(QEvent.MouseMove, local_position,
                                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
            QTest.mouseRelease(bubble.viewport(), Qt.LeftButton, pos=local_position)
            self.assertTrue(bubble.textCursor().hasSelection())
            self.assertEqual(open_url.call_count, 1)
        bubble.close()


if __name__ == '__main__':
    unittest.main()
