import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtTest import QTest
from PyQt5.QtCore import Qt
from pet_floating import PetFloating


class FloatingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.actions = []
        self.panel = PetFloating(self.actions.append)

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_primary_secondary_and_missing_data(self):
        self.panel.set_limits({'rateLimits': {
            'primary': {'usedPercent': 12.5, 'windowDurationMins': 300, 'resetsAt': 1735689600},
            'secondary': {'usedPercent': 43, 'windowDurationMins': 10080},
        }})
        self.assertEqual(self.panel.primary.amount.text(), '已用 12.5%')
        self.assertEqual(self.panel.primary.bar.value(), 12)
        self.assertIn('5 小时', self.panel.primary.caption.text())
        self.assertIn('重置 ', self.panel.primary.reset.text())
        self.assertEqual(self.panel.secondary.amount.text(), '已用 43%')
        self.assertIn('7 天', self.panel.secondary.caption.text())
        self.assertIn('不可用', self.panel.secondary.reset.text())
        self.panel.set_limits({})
        self.assertEqual(self.panel.primary.amount.text(), '不可用')
        self.assertEqual(self.panel.secondary.amount.text(), '不可用')
        self.assertTrue(self.panel.primary.bar.isHidden())

    def test_named_codex_limit_and_invalid_values(self):
        self.panel.set_limits({'rateLimitsByLimitId': {'codex': {
            'primary': {'usedPercent': float('nan'), 'windowDurationMins': -1, 'resetsAt': 'bad'},
            'secondary': {'usedPercent': 101, 'windowDurationMins': True},
        }}})
        self.assertEqual(self.panel.primary.amount.text(), '不可用')
        self.assertEqual(self.panel.secondary.amount.text(), '不可用')
        self.assertEqual(self.panel.primary.reset.text(), '重置时间不可用')
        self.assertIn('窗口不可用', self.panel.secondary.caption.text())
        self.panel.set_limits({'rateLimits': {}, 'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 10}}}})
        self.assertEqual(self.panel.primary.amount.text(), '已用 10%')

    def test_loading_error_clear_old_results_and_actions(self):
        self.panel.set_limits({'rateLimits': {'primary': {'usedPercent': 75}}})
        self.panel.set_loading()
        self.assertEqual(self.panel.primary.amount.text(), '不可用')
        self.assertIn('正在查询', self.panel.state.text())
        error = '错误原因' * 100
        self.panel.set_error(error)
        self.assertIn(error, self.panel.state.toolTip())
        self.assertLess(len(self.panel.state.text()), 170)
        for name in ('chat', 'plan', 'task', 'refresh', 'settings'):
            self.panel.buttons[name].click()
        self.assertEqual(self.actions, ['chat', 'plan', 'task', 'refresh', 'settings'])

    def test_placement_stays_within_available_screen(self):
        pet = QWidget()
        pet.resize(80, 80)
        area = self.app.primaryScreen().availableGeometry()
        pet.move(area.right() - 40, area.bottom() - 40)
        pet.show()
        self.panel.open_for(pet)
        QTest.qWait(25)
        self.assertTrue(area.contains(self.panel.geometry()))
        pet.hide()


if __name__ == '__main__':
    unittest.main()
