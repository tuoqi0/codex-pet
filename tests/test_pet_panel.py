import os
import sys
import tempfile
import shutil
from unittest import mock
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel
import pet_panel
from pet_panel import PetPanel


class FakeBackend:
    def __init__(self, emit):
        self.emit = emit
        self.calls = []

    def login(self): self.calls.append(('login',))
    def account(self): self.calls.append(('account',))
    def limits(self): self.calls.append(('limits',))
    def send(self, *args): self.calls.append(('send', *args))
    def cancel(self): self.calls.append(('cancel',))
    def approve(self, *args): self.calls.append(('approve', *args))
    def close(self): self.calls.append(('close',))


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.actions = []
        self.panel = PetPanel(action_callback=self.actions.append, backend_factory=FakeBackend)

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_chat_progress_and_error_completion(self):
        self.panel.prompt.setPlainText('你好')
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'chat', '你好', None))
        self.panel.bridge.received.emit('delta', '您好')
        self.assertIn('您好', self.panel.transcript.toPlainText())
        self.panel.bridge.received.emit('error', '授權失敗')
        self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'failed'})
        self.assertIn('授權失敗', self.panel.status.text())
        self.assertTrue(self.panel.send_button.isEnabled())

    def test_task_completion_drives_character_and_uses_directory(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.prompt.setPlainText('檢查')
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'task', '檢查', '/tmp'))
        self.assertEqual(self.actions[-1], 'work')
        self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'completed'})
        self.assertEqual(self.actions[-1], 'celebrate')

    def test_bubbles_preserve_plain_text_stream_and_scroll(self):
        self.panel.resize(500, 580)
        self.panel.show()
        self.panel.prompt.setPlainText('<b>普通文字</b>')
        self.panel._send()
        self.panel.bridge.received.emit('delta', '第一段\n')
        self.panel.bridge.received.emit('delta', '很長的回覆。' * 300)
        QTest.qWait(100)
        transcript = self.panel.transcript
        self.assertEqual(transcript.bubbles[-2].toPlainText(), '<b>普通文字</b>')
        self.assertTrue(transcript.bubbles[-1].toPlainText().startswith('第一段\n'))
        self.assertGreater(transcript.verticalScrollBar().maximum(), 0)
        self.assertEqual(transcript.verticalScrollBar().value(), transcript.verticalScrollBar().maximum())
        self.panel.bridge.received.emit('delta', '結尾' * 300)
        transcript.verticalScrollBar().setValue(0)
        QTest.qWait(100)
        self.assertEqual(transcript.verticalScrollBar().value(), 0)

    def test_long_tokens_wrap_and_background_errors_are_separate(self):
        self.panel.resize(500, 580)
        self.panel.show()
        self.panel.prompt.setPlainText('請解釋')
        self.panel._send()
        self.panel.bridge.received.emit('delta', 'x' * 2000)
        self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'completed'})
        QTest.qWait(100)
        transcript = self.panel.transcript
        self.assertEqual(transcript.horizontalScrollBar().maximum(), 0)
        bubble = transcript.bubbles[-1]
        self.assertLessEqual(bubble.document().size().height(), bubble.viewport().height())
        old_reply = transcript.bubbles[-1].toPlainText()
        self.panel.bridge.received.emit('error', '網路問題' * 100)
        QTest.qWait(100)
        self.assertEqual(transcript.bubbles[-2].toPlainText(), old_reply)
        self.assertTrue(transcript.bubbles[-1].toPlainText().startswith('[錯誤]'))
        self.assertLessEqual(self.panel.status.height(), 38)
        self.assertIn('網路問題', self.panel.status.toolTip())

    def test_installed_avatar_is_found_next_to_script(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            source = Path(__file__).resolve().parents[1] / 'assets' / 'dragon-lady.png'
            shutil.copyfile(source, target / 'dragon-lady.png')
            with mock.patch.object(pet_panel, '__file__', str(target / 'pet_panel.py')):
                panel = PetPanel(backend_factory=FakeBackend)
            avatar = panel.findChild(QLabel, 'avatar')
            self.assertIsNotNone(avatar.pixmap())
            self.assertFalse(avatar.pixmap().isNull())
            panel.deleteLater()

    def test_quotas_and_unknown_approvals_fail_closed(self):
        self.panel.refresh_limits()
        self.assertEqual(self.panel.backend.calls, [('account',), ('limits',)])
        self.panel.bridge.received.emit('limits', {'rateLimits': {'primary': {'usedPercent': 25, 'windowDurationMins': 300}}})
        self.assertIn('25%', self.panel.limit_text.text())
        self.assertEqual(self.panel.usage_bars[0][2].value(), 25)
        self.assertFalse(self.panel.usage_bars[0][0].isHidden())
        self.assertTrue(self.panel.usage_bars[1][0].isHidden())
        self.assertIn('不可用', self.panel.limit_text.text())
        self.panel.bridge.received.emit('approval', {'id': 9, 'method': 'unknown', 'params': {}})
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 9, 'decline'))


if __name__ == '__main__':
    unittest.main()
