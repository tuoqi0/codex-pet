import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PyQt5.QtWidgets import QApplication
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

    def test_quotas_and_unknown_approvals_fail_closed(self):
        self.panel.refresh_limits()
        self.assertEqual(self.panel.backend.calls, [('account',), ('limits',)])
        self.panel.bridge.received.emit('limits', {'rateLimits': {'primary': {'usedPercent': 25, 'windowDurationMins': 300}}})
        self.assertIn('25%', self.panel.limit_text.text())
        self.assertIn('不可用', self.panel.limit_text.text())
        self.panel.bridge.received.emit('approval', {'id': 9, 'method': 'unknown', 'params': {}})
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 9, 'decline'))


if __name__ == '__main__':
    unittest.main()
