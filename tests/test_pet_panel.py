import os
import sys
import tempfile
import shutil
import threading
from unittest import mock
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton
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
            source = Path(__file__).resolve().parents[1] / 'assets' / 'dragon-seated.png'
            shutil.copyfile(source, target / 'dragon-seated.png')
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

    def test_quota_events_are_isolated_from_chat_and_refresh_is_coalesced(self):
        events = []
        self.panel.limits_changed.connect(lambda event, data: events.append((event, data)))
        self.panel.prompt.setPlainText('你好')
        self.panel._send()
        before = self.panel.transcript.toPlainText()
        self.assertTrue(self.panel.refresh_limits(include_account=False))
        self.assertFalse(self.panel.refresh_limits(include_account=False))
        self.assertEqual(self.panel.backend.calls.count(('limits',)), 1)
        self.panel.bridge.received.emit('limits_error', '额度查询出错')
        self.assertEqual(self.panel.transcript.toPlainText(), before)
        self.assertTrue(self.panel.busy)
        self.assertFalse(self.panel.limits_inflight)
        self.assertEqual(events[-1], ('limits_error', '额度查询出错'))
        self.assertIn('额度查询出错', self.panel.limit_text.text())
        self.assertTrue(self.panel.refresh_limits(include_account=False))
        self.panel.bridge.received.emit('error', '聊天出错')
        self.assertTrue(self.panel.limits_inflight)
        self.assertEqual(events[-1][0], 'loading')
        self.panel.bridge.received.emit('limits', {'rateLimits': {'primary': {'usedPercent': 20}}})
        self.assertFalse(self.panel.limits_inflight)
        self.assertEqual(events[-1][0], 'limits')
        self.assertIn('20%', self.panel.limit_text.text())

    def test_quota_signal_is_delivered_to_qt_thread(self):
        delivered = []
        main_thread = threading.get_ident()
        self.panel.limits_changed.connect(lambda *event: delivered.append((threading.get_ident(), event)))
        worker = threading.Thread(target=lambda: self.panel.bridge.received.emit('limits', {}))
        worker.start()
        worker.join()
        QTest.qWait(30)
        self.assertEqual(delivered, [(main_thread, ('limits', {}))])

    def test_small_quota_page_keeps_controls_reachable(self):
        self.panel.resize(480, 580)
        self.panel.show()
        self.panel.tabs.setCurrentIndex(1)
        self.panel._show_limits({'rateLimits': {
            'primary': {'usedPercent': 27, 'windowDurationMins': 300, 'resetsAt': 1793278800},
            'secondary': {'usedPercent': 63, 'windowDurationMins': 10080, 'resetsAt': 1793700000},
        }})
        QTest.qWait(30)
        scroll = self.panel.tabs.currentWidget()
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        refresh = next(button for button in scroll.findChildren(QPushButton) if button.text() == '刷新額度')
        scroll.ensureWidgetVisible(refresh)
        QTest.qWait(20)
        self.assertTrue(scroll.viewport().rect().contains(refresh.mapTo(scroll.viewport(), refresh.rect().center())))


if __name__ == '__main__':
    unittest.main()
