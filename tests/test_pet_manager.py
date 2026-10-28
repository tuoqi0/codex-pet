"""Offline integration tests for the actual manager and Qt widgets."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PIL import Image
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
import codex_pet
import companion
import import_bailong
import pet_panel
import weather


class FakeBackend:
    def __init__(self, emit):
        self.emit = emit
        self.limits = mock.Mock()
        self.account = mock.Mock()
        self.models = mock.Mock()
        self.close = mock.Mock()


class FakeWeather:
    def __init__(self):
        self.requests = []

    def current(self, location, callback):
        self.requests.append((location, callback))


class ManagerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        assets = root / 'bailong'
        assets.mkdir()
        Image.new('RGBA', (10, 10), 'red').save(assets / 'idle.webp')
        variants = [{'id': ident, 'file': 'idle.webp'} for ident in ('01', '04', '08', '14', '17', '20', 'quota')]
        (assets / '素材清单.json').write_text(json.dumps({'idle': {'id': 'idle', 'file': 'idle.webp'}, 'variants': variants}), encoding='utf8')
        store = companion.CompanionSettingsStore(root / 'settings.json')
        fake_weather = FakeWeather()
        real_panel = pet_panel.PetPanel
        patches = [
            mock.patch.object(companion, 'CompanionSettingsStore', return_value=store),
            mock.patch.object(import_bailong, 'default_destination', return_value=assets),
            mock.patch.object(weather, 'WeatherClient', return_value=fake_weather),
            mock.patch.object(pet_panel, 'PetPanel', side_effect=lambda **kw: real_panel(backend_factory=FakeBackend, **kw)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.manager = codex_pet.PetManager()
        self.manager.companion.settings.quiet_start = '00:00'
        self.manager.companion.settings.quiet_end = '00:00'
        self.manager.interaction_timer.stop()
        self.manager.companion_timer.stop()
        self.fake_weather = fake_weather
        self.app.processEvents()

    def tearDown(self):
        if self.manager.panel:
            self.manager.panel.close()
            self.manager.panel.deleteLater()
        if self.manager.floating:
            self.manager.floating.close()
            self.manager.floating.deleteLater()
        self.manager.dragon.close()
        self.manager.qpet.close()
        self.app.processEvents()
        self.temp.cleanup()

    def test_double_click_quota_async_and_mode_navigation(self):
        manager = self.manager
        pet = manager.dragon
        if not pet.wayland and pet.x() >= manager.qpet.width() + 14:
            self.assertLess(manager.qpet.frameGeometry().right(), pet.frameGeometry().left())
        QTest.mouseDClick(pet, Qt.LeftButton, pos=QPoint(100, 80))
        self.assertTrue(manager.floating.isVisible())
        panel = manager.panel
        panel.backend.limits.assert_called_once()
        panel.backend.account.assert_not_called()
        self.assertIn('查询', manager.floating.state.text())
        payload = {'rateLimits': {'primary': {'usedPercent': 32, 'windowDurationMins': 300, 'resetsAt': 2000000000},
                                  'secondary': {'usedPercent': 71, 'windowDurationMins': 10080, 'resetsAt': 2000000000}}}
        thread = threading.Thread(target=lambda: panel.backend.emit('limits', payload))
        thread.start()
        thread.join()
        QTest.qWait(30)
        self.assertEqual(manager.floating.primary.amount.text(), '已用 32%')
        self.assertEqual(manager.floating.secondary.amount.text(), '已用 71%')
        for mode in ('chat', 'plan', 'task'):
            manager.floating.show()
            manager.floating.buttons[mode].click()
            self.assertTrue(panel.isVisible())
            self.assertFalse(manager.floating.isVisible())
            self.assertEqual(panel.mode.currentData(), mode)
            self.assertEqual(panel.tabs.currentIndex(), 0)
        # A second opening from the other pet triggers a single new refresh.
        QTest.mouseDClick(manager.qpet, Qt.LeftButton, pos=QPoint(70, 70))
        self.assertEqual(panel.backend.limits.call_count, 2)

    def test_open_panel_shows_existing_inflight_quota_loading(self):
        manager = self.manager
        panel = manager._ensure_panel()
        self.assertTrue(panel.refresh_limits())
        self.assertTrue(panel.limits_inflight)
        manager.open_floating(manager.dragon)
        panel.backend.limits.assert_called_once()
        self.assertIn('正在查询', manager.floating.state.text())

    def test_quiet_disabled_paused_and_busy_prevent_pet_conversation(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(260, 100)
        settings = companion.CompanionSettings(quiet_start='00:00', quiet_end='00:00')
        manager._save_settings(settings)
        panel = manager._ensure_panel()
        with mock.patch('codex_pet.datetime') as wall:
            wall.now.return_value = datetime(2026, 10, 28, 12, 0)
            self.assertTrue(manager.interaction_ready())
            cases = ('quiet', 'disabled', 'paused', 'busy')
            for case in cases:
                with self.subTest(case=case):
                    manager.companion.settings.enabled = case != 'disabled'
                    manager.companion.settings.quiet_start = '11:00' if case == 'quiet' else '00:00'
                    manager.companion.settings.quiet_end = '13:00' if case == 'quiet' else '00:00'
                    manager.companion.paused_until = float('inf') if case == 'paused' else 0
                    panel.busy = case == 'busy'
                    manager.next_interaction = 0
                    manager.maybe_interact()
                    self.assertFalse(manager.dragon.speech.isVisible())
                    self.assertFalse(manager.qpet.speech.isVisible())
                    self.assertEqual(manager.dragon.behavior.message, '')
                    self.assertEqual(manager.qpet.player.current, 'idle')

    def test_user_greeting_gets_a_reply_from_other_nearby_pet(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(260, 100)
        manager.dragon.react('你好')
        with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.on_user_greet(manager.dragon)
            delayed.call_args.args[1]()
            self.assertEqual(manager.qpet.message, '我也在，和你一起！')
        manager.qpet.player.play('idle')
        manager.dragon.behavior.reaction_until = 0
        manager.qpet.respond('你好', '01')
        with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.on_user_greet(manager.qpet)
            delayed.call_args.args[1]()
            self.assertEqual(manager.dragon.behavior.message, '我也在這裡陪你。')

    def test_quota_does_not_interrupt_work_animation(self):
        manager = self.manager
        manager.play_action('work')
        self.assertEqual(manager.dragon.behavior.activity, 'work')
        with mock.patch.object(manager.qpet.player, 'play') as play:
            manager.play_action('quota')
            play.assert_not_called()
        self.assertEqual(manager.dragon.behavior.activity, 'work')
        manager.play_action('celebrate')
        self.assertEqual(manager.dragon.behavior.activity, 'idle')
        self.assertEqual(manager.dragon.behavior.pose(codex_pet.time.monotonic()), 'happy')

    def test_floating_weather_action_starts_manual_refresh(self):
        manager = self.manager
        manager.open_floating(manager.dragon)
        manager._floating_action('weather')
        self.assertIn('选择城市', manager.weather_text)
        manager._save_settings(companion.CompanionSettings(weather_enabled=True,
            weather_label='Dalian', weather_latitude=38.91, weather_longitude=121.61))
        manager._floating_action('weather')
        self.assertEqual(len(self.fake_weather.requests), 1)
        self.assertIn('正在查询', manager.weather_text)
        manager._floating_action('weather')
        self.assertIn('正在查询', manager.weather_text)
        self.assertEqual(len(self.fake_weather.requests), 1)
        self.fake_weather.requests[-1][1]('weather', {'description': '晴', 'temperature': 18,
            'temperature_unit': '°C', 'source': 'Open-Meteo', 'time': '2026-10-28T12:00', 'timezone': 'Asia/Shanghai'})
        self.app.processEvents()
        self.assertIn('晴', manager.weather_text)
        self.assertIn('Open-Meteo', manager.weather_detail)

    def test_companion_event_dispatches_to_both_or_q_alone(self):
        manager = self.manager
        event = companion.CompanionEvent('water', '记得喝水。', 'water')
        with mock.patch.object(manager.companion, 'tick', return_value=event), \
                mock.patch.object(manager.qpet.player, 'play', wraps=manager.qpet.player.play) as play, \
                mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.companion_tick()
            self.assertEqual(manager.dragon.behavior.message, event.message)
            play.assert_any_call('08')
            delayed.call_args.args[1]()
            self.assertTrue(manager.qpet.message)
        manager.dragon.hide()
        manager.qpet.message = ''
        manager.qpet.message_until = 0
        manager.qpet.player.play('idle')
        with mock.patch.object(manager.companion, 'tick', return_value=event), \
                mock.patch.object(manager.qpet, 'respond', wraps=manager.qpet.respond) as respond:
            manager.companion_tick()
            respond.assert_called_once_with(event.message, '08')
            self.assertEqual(manager.qpet.message, event.message)

    def test_real_scheduler_reminder_when_only_q_visible(self):
        manager = self.manager
        manager.dragon.hide()
        settings = companion.CompanionSettings(breaks=False, water=True, meals=False,
                                               greetings=False, water_minutes=15,
                                               quiet_start='00:00', quiet_end='00:00')
        manager.companion = companion.CompanionScheduler(1000, settings)
        with mock.patch('codex_pet.time.monotonic') as clock, \
                mock.patch('codex_pet.datetime') as wall, \
                mock.patch.object(manager.q_idle, 'tick'), \
                mock.patch.object(manager.qpet, 'respond', wraps=manager.qpet.respond) as respond:
            wall.now.return_value = datetime(2026, 10, 28, 10, 0)
            for elapsed in range(60, 1201, 60):
                clock.return_value = 1000 + elapsed
                manager.companion_tick()
            respond.assert_called_once()
            self.assertEqual(respond.call_args.args[1], '08')
            self.assertTrue(manager.qpet.message)

    def test_weather_opt_in_results_errors_and_old_result_filter(self):
        manager = self.manager
        manager.weather_due = 0
        manager.companion_tick()
        self.assertEqual(self.fake_weather.requests, [])
        settings = companion.CompanionSettings(weather_enabled=True, weather_label='Paris, France',
                                               weather_latitude=48.86, weather_longitude=2.35,
                                               quiet_start='00:00', quiet_end='00:00')
        manager._save_settings(settings)
        manager._request_weather()
        location, first = self.fake_weather.requests[-1]
        self.assertEqual(location['name'], 'Paris, France')
        self.assertEqual(location['latitude'], 48.86)
        data = {'description': '多云', 'temperature': 18, 'temperature_unit': '°C',
                'source': 'Open-Meteo（天气模型）', 'time': '2026-10-28T12:00', 'timezone': 'Europe/Paris'}
        thread = threading.Thread(target=lambda: first('weather', data))
        thread.start()
        thread.join()
        QTest.qWait(30)
        self.assertIn('Paris, France 多云 18°C', manager.weather_text)
        self.assertIn('模型时间', manager.weather_detail)
        manager._request_weather()
        stale = self.fake_weather.requests[-1][1]
        manager._save_settings(companion.CompanionSettings(weather_enabled=False))
        stale('weather', data)
        self.app.processEvents()
        self.assertEqual(manager.weather_text, '天气：未启用')
        manager._save_settings(settings)
        manager._request_weather()
        self.fake_weather.requests[-1][1]('error', {'message': '网络不可用'})
        self.app.processEvents()
        self.assertEqual(manager.weather_text, '天气：暂时不可用')
        self.assertEqual(manager.weather_detail, '网络不可用')
        self.assertFalse(manager.weather_inflight)

    def test_pause_keeps_weather_data_fresh_without_late_weather_speech(self):
        manager = self.manager
        settings = companion.CompanionSettings(weather_enabled=True, weather_label='Paris',
                                               weather_latitude=48.86, weather_longitude=2.35,
                                               quiet_start='00:00', quiet_end='00:00')
        manager._save_settings(settings)
        manager._request_weather()
        callback = self.fake_weather.requests[-1][1]
        manager.companion.snooze(codex_pet.time.monotonic(), 60)
        manager.weather_due = 0
        callback('weather', {'description': '晴天', 'temperature': 18, 'temperature_unit': '°C',
                             'source': 'Open-Meteo', 'time': '2026-10-28T12:00', 'timezone': 'Europe/Paris'})
        self.app.processEvents()
        self.assertFalse(manager.dragon.speech.isVisible())
        self.assertFalse(manager.qpet.speech.isVisible())
        self.assertIn('晴天', manager.weather_text)
        manager.companion_tick()
        self.assertEqual(len(self.fake_weather.requests), 2)
        self.assertFalse(manager.refresh_weather())  # coalesces concurrent requests

    def test_weather_refresh_ignores_quiet_hours_disabled_reminders_and_busy(self):
        manager = self.manager
        settings = companion.CompanionSettings(enabled=False, weather_enabled=True,
            weather_label='Dalian', weather_latitude=38.91, weather_longitude=121.61,
            quiet_start='00:00', quiet_end='23:59')
        manager._save_settings(settings)
        manager.weather_due = 0
        manager._ensure_panel().busy = True
        manager.companion_tick()
        self.assertEqual(len(self.fake_weather.requests), 1)
        self.fake_weather.requests[-1][1]('error', {'message': '网络不可用'})
        self.app.processEvents()
        self.assertEqual(manager.weather_text, '天气：暂时不可用')
        self.assertGreater(manager.weather_due, codex_pet.time.monotonic() + 250)
        manager.companion_tick()
        self.assertEqual(len(self.fake_weather.requests), 1)
        self.assertTrue(manager.refresh_weather())
        self.assertEqual(len(self.fake_weather.requests), 2)

    def test_reminder_and_weather_use_complete_bubbles_and_hide_with_pets(self):
        manager = self.manager
        text = '我陪你有一阵子了，起来舒展一下，也记得多喝水和按时吃饭哦。'
        event = companion.CompanionEvent('break', text, 'break')
        with mock.patch.object(manager.companion, 'tick', return_value=event), \
                mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.companion_tick()
            self.assertTrue(manager.dragon.speech.isVisible())
            self.assertEqual(manager.dragon.speech.message.text(), text)
            delayed.call_args.args[1]()
            self.assertTrue(manager.qpet.speech.isVisible())
            self.assertTrue(manager.qpet.speech.message.text())
        manager.dragon.hide()
        manager.qpet.hide()
        self.assertFalse(manager.dragon.speech.isVisible())
        self.assertFalse(manager.qpet.speech.isVisible())
        manager.dragon.show()
        manager.dragon.behavior.reaction_until = 0
        settings = companion.CompanionSettings(weather_enabled=True, weather_label='巴黎，法兰西岛，法国',
                                               weather_latitude=48.86, weather_longitude=2.35,
                                               quiet_start='00:00', quiet_end='00:00')
        manager._save_settings(settings)
        manager._request_weather()
        self.fake_weather.requests[-1][1]('weather', {'description': '局部多云并有小阵雨', 'temperature': 18,
            'temperature_unit': '°C', 'source': 'Open-Meteo', 'time': '2026-10-28T12:00', 'timezone': 'Europe/Paris'})
        self.app.processEvents()
        self.assertTrue(manager.dragon.speech.isVisible())
        self.assertEqual(manager.dragon.speech.message.text(),
                         '巴黎，法兰西岛，法国 局部多云并有小阵雨 18°C，出门记得看天气哦。')
        manager.dragon.hide()
        self.assertFalse(manager.dragon.speech.isVisible())


if __name__ == '__main__':
    unittest.main()
