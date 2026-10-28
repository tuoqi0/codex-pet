import os
import sys
import unittest
from unittest import mock
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtTest import QTest
from PyQt5.QtCore import QRect, Qt
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
        for name in ('chat', 'plan', 'task', 'refresh', 'weather', 'settings'):
            self.panel.buttons[name].click()
        self.assertEqual(self.actions, ['chat', 'plan', 'task', 'refresh', 'weather', 'settings'])

    def test_weather_states_and_refresh_are_visible(self):
        self.panel.show()
        self.panel.set_weather('天气：正在查询…')
        self.assertIn('正在查询', self.panel.weather.text())
        self.assertTrue(self.panel.weather_detail.isHidden())
        self.panel.set_weather('天气：大连 局部多云 13.6°C', 'Open-Meteo · 模型时间 2026-10-28 18:45')
        self.assertIn('大连', self.panel.weather.text())
        self.assertIn('模型时间', self.panel.weather_detail.text())
        self.assertTrue(self.panel.weather_detail.isVisible())
        self.panel.set_weather('天气：暂时不可用', '无法连接天气服务，请检查网络。')
        self.assertIn('不可用', self.panel.weather.text())
        self.assertIn('检查网络', self.panel.weather_detail.text())
        self.panel.buttons['weather'].click()
        self.assertEqual(self.actions, ['weather'])

    def test_placement_stays_within_available_screen(self):
        pet = QWidget()
        pet.resize(80, 80)
        area = self.app.primaryScreen().availableGeometry()
        pet.move(area.right() - 40, area.bottom() - 40)
        pet.show()
        with mock.patch.object(QApplication, 'platformName', return_value='xcb'):
            self.panel.open_for(pet)
            QTest.qWait(25)
        self.assertTrue(area.contains(self.panel.geometry()))
        pet.hide()

    def test_async_content_updates_stay_within_small_x11_screen(self):
        area = QRect(0, 0, 500, 350)
        screen = mock.Mock()
        screen.availableGeometry.return_value = area
        pet = QWidget()
        pet.resize(60, 60)
        pet.move(420, 280)
        pet.show()
        with mock.patch.object(QApplication, 'platformName', return_value='xcb'), \
                mock.patch.object(QApplication, 'screenAt', return_value=screen):
            self.panel.open_for(pet)
            for update in (lambda: self.panel.set_loading(),
                           lambda: self.panel.set_limits({'rateLimits': {
                               'primary': {'usedPercent': 12, 'windowDurationMins': 300},
                               'secondary': {'usedPercent': 33, 'windowDurationMins': 10080}}}),
                           lambda: self.panel.set_weather('较长的城市名称' * 8),
                           lambda: self.panel.set_error('天气网络查询失败' * 30)):
                update()
                QTest.qWait(30)
                self.assertTrue(area.contains(self.panel.frameGeometry()), str(self.panel.frameGeometry()))
            self.assertGreater(self.panel.scroll.verticalScrollBar().maximum(), 0)
            self.panel.scroll.ensureWidgetVisible(self.panel.buttons['settings'])
            QTest.qWait(20)
            button = self.panel.buttons['settings']
            self.assertTrue(self.panel.scroll.viewport().rect().contains(button.mapTo(self.panel.scroll.viewport(), button.rect().center())))
            self.panel.scroll.ensureWidgetVisible(self.panel.buttons['weather'])
            QTest.qWait(20)
            button = self.panel.buttons['weather']
            self.assertTrue(self.panel.scroll.viewport().rect().contains(button.mapTo(self.panel.scroll.viewport(), button.rect().center())))
        pet.hide()

    def test_normal_x11_screen_has_no_scrollbar_or_clipped_controls(self):
        screen = mock.Mock()
        area = QRect(0, 0, 1920, 1080)
        screen.availableGeometry.return_value = area
        pet = QWidget()
        pet.resize(80, 80)
        pet.move(1820, 950)
        pet.show()
        with mock.patch.object(QApplication, 'platformName', return_value='xcb'), \
                mock.patch.object(QApplication, 'screenAt', return_value=screen):
            self.panel.set_loading()
            self.panel.open_for(pet)
            for update in (lambda: self.panel.set_loading(),
                           lambda: self.panel.set_limits({'rateLimits': {
                               'primary': {'usedPercent': 12, 'windowDurationMins': 300},
                               'secondary': {'usedPercent': 33, 'windowDurationMins': 10080}}}),
                           lambda: self.panel.set_weather('天气：大连 晴朗 18°C')):
                update()
                QTest.qWait(30)
                self.assertTrue(area.contains(self.panel.frameGeometry()))
                self.assertEqual(self.panel.scroll.verticalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
                self.assertEqual(self.panel.scroll.verticalScrollBar().maximum(), 0)
                for button in self.panel.buttons.values():
                    center = button.mapTo(self.panel.scroll.viewport(), button.rect().center())
                    self.assertTrue(self.panel.scroll.viewport().rect().contains(center))
        pet.hide()

    def test_wayland_never_positions_on_open_or_update(self):
        pet = QWidget()
        with mock.patch.object(QApplication, 'platformName', return_value='wayland'), \
                mock.patch.object(self.panel, 'move', wraps=self.panel.move) as move:
            self.panel.open_for(pet)
            self.panel.set_error('出错')
            QTest.qWait(20)
            move.assert_not_called()


if __name__ == '__main__':
    unittest.main()
