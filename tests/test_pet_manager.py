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
from PyQt5.QtCore import QPoint, QPointF, QRect, Qt
from PyQt5.QtGui import QContextMenuEvent, QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMenu
import codex_pet
import companion
import import_bailong
import pet_panel
import weather
import window_preferences


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
        variants = [{'id': ident, 'file': 'idle.webp'} for ident in
                    ('01', '04', '08', '14', '17', '18', '20', 'quota', 'work02', 'drag', 'landing')]
        (assets / '素材清单.json').write_text(json.dumps({'idle': {'id': 'idle', 'file': 'idle.webp'}, 'variants': variants}), encoding='utf8')
        store = companion.CompanionSettingsStore(root / 'settings.json')
        self.window_store = window_preferences.WindowPreferencesStore(root / 'windows.json')
        fake_weather = FakeWeather()
        real_panel = pet_panel.PetPanel
        patches = [
            mock.patch.object(companion, 'CompanionSettingsStore', return_value=store),
            mock.patch.object(window_preferences, 'WindowPreferencesStore', return_value=self.window_store),
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

    def test_window_menu_actions_and_drag_persist_across_restart(self):
        manager = self.manager
        self.assertFalse(self.window_store.path.exists())  # construction never writes
        menu = QMenu()
        manager.populate_menu(menu, manager.dragon)
        size_menu = next(a.menu() for a in menu.actions() if a.text() == '角色大小')
        next(a for a in size_menu.actions() if a.text() == '大').trigger()
        self.assertAlmostEqual(manager.dragon.scale_factor, .7875)

        start, end = QPoint(30, 30), QPoint(75, 60)
        QTest.mousePress(manager.dragon, Qt.LeftButton, pos=start)
        move = QMouseEvent(QMouseEvent.MouseMove, QPointF(end), QPointF(manager.dragon.mapToGlobal(end)),
                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(manager.dragon, move)
        QTest.mouseRelease(manager.dragon, Qt.LeftButton, pos=end)
        position = (manager.dragon.x(), manager.dragon.y())
        manager.set_patrol(True)

        menu = QMenu()
        manager.populate_menu(menu, manager.qpet)
        next(a for a in menu.actions() if a.text() == '隐藏这个角色').trigger()
        self.assertFalse(manager.qpet.isVisible())
        prefs, warning = self.window_store.load()
        self.assertIsNone(warning)
        self.assertEqual((prefs.dragon.x, prefs.dragon.y), position)
        self.assertAlmostEqual(prefs.dragon.scale, .7875)
        self.assertTrue(prefs.dragon_patrol)
        self.assertFalse(prefs.qpet.visible)

        manager.interaction_timer.stop()
        manager.companion_timer.stop()
        manager.dragon.close()
        manager.qpet.close()
        self.manager = codex_pet.PetManager()
        self.manager.interaction_timer.stop()
        self.manager.companion_timer.stop()
        self.app.processEvents()
        self.assertEqual((self.manager.dragon.x(), self.manager.dragon.y()), position)
        self.assertAlmostEqual(self.manager.dragon.scale_factor, .7875)
        self.assertTrue(self.manager.dragon.behavior.patrol)
        self.assertFalse(self.manager.qpet.isVisible())
        menu = QMenu()
        self.manager.populate_menu(menu, self.manager.dragon)
        next(a for a in menu.actions() if a.text() == '显示另一个角色').trigger()
        self.assertTrue(self.manager.qpet.isVisible())
        self.assertTrue(self.window_store.load()[0].qpet.visible)

    def test_bad_window_settings_warn_without_startup_write_then_allow_explicit_change(self):
        manager = self.manager
        manager.interaction_timer.stop()
        manager.companion_timer.stop()
        manager.dragon.close()
        manager.qpet.close()
        self.window_store.path.write_text('{broken', encoding='utf-8')
        self.manager = codex_pet.PetManager()
        self.manager.interaction_timer.stop()
        self.manager.companion_timer.stop()
        self.assertEqual(self.window_store.path.read_text(), '{broken')
        self.assertTrue(self.manager.dragon.speech.isVisible())
        self.assertIn('原文件已保留', self.manager.dragon.speech.message.text())
        self.manager.set_pet_scale(self.manager.qpet, .75)
        self.assertEqual(self.window_store.load()[0].qpet.scale, .75)
        self.assertEqual(self.window_store.backup_path.read_text(), '{broken')
        self.assertIn('已备份', self.manager.dragon.speech.message.text())

    def test_missing_q_asset_still_shows_dragon_without_rewriting_saved_visibility(self):
        prefs = window_preferences.WindowPreferences(
            dragon=window_preferences.PetWindowPreference(scale=.63, visible=False))
        self.window_store.save(prefs)
        previous = self.window_store.path.read_bytes()
        with mock.patch.object(import_bailong, 'default_destination',
                               return_value=Path(self.temp.name) / 'not-present'):
            single = codex_pet.PetManager()
        try:
            self.assertIsNone(single.qpet)
            self.assertTrue(single.dragon.isVisible())
            self.assertEqual(self.window_store.path.read_bytes(), previous)
            with self.assertRaises(ValueError):
                single.set_pet_visible(single.dragon, False)
            single.set_pet_scale(single.dragon, .75)
            saved, _ = self.window_store.load()
            self.assertFalse(saved.dragon.visible)
            self.assertTrue(saved.qpet.visible)
        finally:
            single.interaction_timer.stop()
            single.companion_timer.stop()
            single.dragon.close()

    def test_wayland_drag_does_not_overwrite_previous_x11_position(self):
        manager = self.manager
        old = window_preferences.PetWindowPreference(12, 34, .63)
        manager.window_preferences = codex_pet.replace(manager.window_preferences, dragon=old)
        manager.dragon.wayland = True
        manager.dragon.move(600, 400)
        manager.window_moved(manager.dragon)
        self.assertEqual(manager.window_preferences.dragon, old)
        self.assertFalse(self.window_store.path.exists())

    def test_scale_and_drag_clamp_to_available_multi_monitor_work_area(self):
        manager = self.manager
        left = mock.Mock()
        right = mock.Mock()
        left.availableGeometry.return_value = QRect(0, 0, 800, 600)
        right.availableGeometry.return_value = QRect(800, 0, 800, 600)
        def screen_at(point):
            return left if point.x() < 800 else right if point.x() < 1600 else None
        with mock.patch.object(QApplication, 'screens', return_value=[left, right]), \
                mock.patch.object(QApplication, 'primaryScreen', return_value=left), \
                mock.patch.object(QApplication, 'screenAt', side_effect=screen_at):
            manager.dragon.move(1550, 550)
            manager.set_pet_scale(manager.dragon, .7875)
            rect = manager.dragon.geometry()
            self.assertTrue(right.availableGeometry().contains(rect))
            manager.dragon.move(5000, 900)
            manager.window_moved(manager.dragon)
            self.assertTrue(right.availableGeometry().contains(manager.dragon.geometry()))
            prefs, _ = self.window_store.load()
            self.assertEqual((prefs.dragon.x, prefs.dragon.y), (manager.dragon.x(), manager.dragon.y()))

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
            self.assertEqual(manager.dragon.behavior.message, '我也在这里陪你。')

    def test_wayland_explicit_greetings_can_reply_but_automatic_chat_is_disabled(self):
        manager = self.manager
        manager.dragon.wayland = manager.qpet.wayland = True
        manager.dragon.move(0, 0)
        manager.qpet.move(10000, 10000)  # coordinates are meaningless on Wayland
        self.assertFalse(manager.interaction_ready())
        with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.dragon.greet()
            delayed.assert_called_once()
            delayed.call_args.args[1]()
            self.assertEqual(manager.qpet.message, '我也在，和你一起！')
        manager.qpet.player.play('idle')
        manager.dragon.behavior.reaction_until = 0
        with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.qpet.greet()
            delayed.assert_called_once()
            delayed.call_args.args[1]()
            self.assertEqual(manager.dragon.behavior.message, '我也在这里陪你。')
        self.assertFalse(manager.interaction_ready())

    def test_context_menu_greetings_use_shared_greeting_flow(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(260, 100)
        class TestMenu(codex_pet.QMenu):
            def exec_(self, _position):
                next(action for action in self.actions() if action.text() == '打招呼').trigger()

        for source in (manager.dragon, manager.qpet):
            manager.dragon.behavior.reaction_until = 0
            manager.qpet.player.play('idle')
            manager.dragon.speech.hide()
            manager.qpet.speech.hide()
            with self.subTest(source=source.windowTitle()), \
                    mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed, \
                    mock.patch.object(codex_pet, 'QMenu', TestMenu):
                source.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(10, 10)))
                self.assertTrue(source.speech.isVisible())
                delayed.assert_called_once()
                delayed.call_args.args[1]()
                other = manager.qpet if source is manager.dragon else manager.dragon
                self.assertTrue(other.speech.isVisible())
                self.assertFalse(source.speech.geometry().intersects(other.speech.geometry()))

    def test_dragging_hides_old_bubble_for_each_pet(self):
        for pet in (self.manager.dragon, self.manager.qpet):
            with self.subTest(pet=pet.windowTitle()):
                pet.speech.show_message('一条提醒', pet)
                local = QPoint(30, 30)
                QTest.mousePress(pet, Qt.LeftButton, pos=local)
                moved = local + QPoint(QApplication.startDragDistance() + 10, 0)
                event = QMouseEvent(QMouseEvent.MouseMove, QPointF(moved),
                                    QPointF(pet.mapToGlobal(moved)), Qt.NoButton,
                                    Qt.LeftButton, Qt.NoModifier)
                QApplication.sendEvent(pet, event)
                self.assertTrue(pet.drag_moved)
                self.assertFalse(pet.speech.isVisible())
                QTest.mouseRelease(pet, Qt.LeftButton, pos=moved)

    def test_automatic_conversation_bubbles_do_not_overlap(self):
        manager = self.manager
        manager.next_interaction = 0
        with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.maybe_interact()
            delayed.assert_called_once()
            delayed.call_args.args[1]()
        self.assertTrue(manager.dragon.speech.isVisible())
        self.assertTrue(manager.qpet.speech.isVisible())
        self.assertFalse(manager.dragon.speech.geometry().intersects(manager.qpet.speech.geometry()))

    def test_automatic_conversation_alternates_speaker_and_reacts(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(260, 100)
        with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.next_interaction = 0
            manager.maybe_interact()
            self.assertTrue(manager.dragon.speech.isVisible())
            self.assertEqual(manager.next_interaction_speaker, 'qpet')
            delayed.call_args.args[1]()
            self.assertTrue(manager.qpet.speech.isVisible())
            manager.dragon.behavior.reaction_until = 0
            manager.dragon.speech.hide()
            manager.qpet.speech.hide()
            manager.qpet.player.play('idle')
            manager.next_interaction = 0
            old_dragon_serial = manager.dragon.reaction_serial
            delayed.reset_mock()
            manager.maybe_interact()
            delayed.assert_called_once()
            self.assertTrue(manager.qpet.speech.isVisible())
            self.assertEqual(manager.dragon.reaction_serial, old_dragon_serial)
            self.assertEqual(manager.next_interaction_speaker, 'dragon')
            delayed.call_args.args[1]()
            self.assertEqual(manager.dragon.reaction_serial, old_dragon_serial + 1)
            self.assertTrue(manager.dragon.speech.isVisible())
            self.assertFalse(manager.dragon.speech.geometry().intersects(manager.qpet.speech.geometry()))

    def test_q_conversation_reply_is_cancelled_when_context_changes(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(260, 100)
        panel = manager._ensure_panel()
        for case in ('new_speech', 'busy', 'hidden', 'dragging', 'quiet', 'far'):
            with self.subTest(case=case), mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
                manager.companion.settings.enabled = True
                manager.companion.settings.quiet_start = '00:00'
                manager.companion.settings.quiet_end = '00:00'
                manager.dragon.behavior.reaction_until = 0
                manager.dragon.speech.hide()
                manager.dragon.show()
                manager.dragon.press_global = None
                manager.qpet.show()
                manager.qpet.speech.hide()
                manager.qpet.player.play('idle')
                manager.qpet.move(260, 100)
                panel.busy = False
                manager.next_interaction_speaker = 'qpet'
                manager.next_interaction = 0
                manager.maybe_interact()
                delayed.assert_called_once()
                callback = delayed.call_args.args[1]
                serial = manager.dragon.reaction_serial
                if case == 'new_speech':
                    # Even an identical text is a new reaction and invalidates the old callback.
                    manager.qpet.respond(manager.qpet.message, manager.qpet.player.current)
                elif case == 'busy':
                    panel.busy = True
                elif case == 'hidden':
                    manager.dragon.hide()
                elif case == 'dragging':
                    manager.dragon.press_global = QPoint(1, 1)
                elif case == 'quiet':
                    manager.companion.settings.enabled = False
                else:
                    manager.qpet.move(1500, 100)
                callback()
                self.assertEqual(manager.dragon.reaction_serial, serial)
        manager.dragon.show()
        manager.dragon.press_global = None
        panel.busy = False

    def test_autonomous_conversation_waits_for_existing_bubbles(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(260, 100)
        for pet in (manager.dragon, manager.qpet):
            with self.subTest(pet=pet.windowTitle()):
                pet.speech.show_message('还在显示的消息', pet)
                self.assertFalse(manager.interaction_ready())
                manager.next_interaction = 0
                with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
                    manager.maybe_interact()
                    delayed.assert_not_called()
                pet.speech.hide()

    def test_repeated_identical_user_greeting_cancels_previous_reply(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(260, 100)
        with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.dragon.react('你好')
            manager.on_user_greet(manager.dragon)
            old_reply = delayed.call_args.args[1]
            manager.dragon.react('你好')
            manager.on_user_greet(manager.dragon)
            new_reply = delayed.call_args.args[1]
            old_reply()
            self.assertEqual(manager.qpet.message, '')
            new_reply()
            self.assertEqual(manager.qpet.message, '我也在，和你一起！')

    def test_q_conversation_failed_animation_does_not_schedule_reply(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(260, 100)
        manager.next_interaction_speaker = 'qpet'
        manager.next_interaction = 0
        with mock.patch.object(manager.qpet, 'respond', return_value=False), \
                mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            manager.maybe_interact()
            delayed.assert_not_called()
            self.assertEqual(manager.next_interaction_speaker, 'qpet')
            self.assertGreater(manager.next_interaction, codex_pet.time.monotonic())

    def test_click_reply_still_works_when_automatic_companionship_is_suppressed(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(340, 100)
        with mock.patch('codex_pet.datetime') as wall, \
                mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            wall.now.return_value = datetime(2026, 10, 28, 12, 0)
            for case in ('quiet', 'disabled', 'paused'):
                for source in (manager.dragon, manager.qpet):
                    with self.subTest(case=case, source=source.windowTitle()):
                        settings = manager.companion.settings
                        settings.enabled = case != 'disabled'
                        settings.quiet_start = '11:00' if case == 'quiet' else '00:00'
                        settings.quiet_end = '13:00' if case == 'quiet' else '00:00'
                        manager.companion.paused_until = float('inf') if case == 'paused' else 0
                        manager.dragon.behavior.reaction_until = 0
                        manager.dragon.behavior.message = ''
                        manager.qpet.message = ''
                        manager.qpet.player.play('idle')
                        delayed.reset_mock()
                        QTest.mouseClick(source, Qt.LeftButton, pos=QPoint(70, 70))
                        QTest.qWait(QApplication.doubleClickInterval() + 100)
                        delayed.assert_called_once()
                        self.assertEqual(delayed.call_args.args[0], 1200)
                        delayed.call_args.args[1]()
                        if source is manager.dragon:
                            self.assertEqual(manager.qpet.message, '我也在，和你一起！')
                        else:
                            self.assertEqual(manager.dragon.behavior.message, '我也在这里陪你。')
                        self.assertFalse(manager.interaction_ready())

    def test_click_reply_rechecks_busy_hidden_and_dragging_before_delayed_response(self):
        manager = self.manager
        manager.dragon.move(20, 100)
        manager.qpet.move(340, 100)
        panel = manager._ensure_panel()
        with mock.patch.object(codex_pet.QTimer, 'singleShot') as delayed:
            for case in ('busy', 'hidden', 'dragging'):
                with self.subTest(case=case):
                    manager.dragon.behavior.reaction_until = 0
                    manager.qpet.player.play('idle')
                    manager.qpet.message = ''
                    manager.qpet.show()
                    manager.qpet.press_global = None
                    panel.busy = False
                    delayed.reset_mock()
                    QTest.mouseClick(manager.dragon, Qt.LeftButton, pos=QPoint(70, 70))
                    QTest.qWait(QApplication.doubleClickInterval() + 100)
                    delayed.assert_called_once()
                    if case == 'busy':
                        panel.busy = True
                    elif case == 'hidden':
                        manager.qpet.hide()
                    else:
                        manager.qpet.press_global = QPoint(1, 1)
                    delayed.call_args.args[1]()
                    self.assertEqual(manager.qpet.message, '')
            panel.busy = False
            manager.qpet.press_global = None
            manager.qpet.show()

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

    def test_work_greeting_returns_to_work_until_task_completes(self):
        manager = self.manager
        qpet = manager.qpet
        manager.play_action('work')
        self.assertEqual(qpet.player.current, 'work02')
        with mock.patch.object(codex_pet.QTimer, 'singleShot'):
            qpet.greet()
        self.assertEqual(qpet.player.current, '01')
        self.assertTrue(qpet.speech.isVisible())
        manager.play_action('quota')
        self.assertEqual(qpet.player.current, '01')
        qpet.player.advance()
        self.assertEqual(qpet.player.current, 'work02')
        with mock.patch.object(codex_pet.QTimer, 'singleShot'):
            qpet.greet()
        manager.play_action('idle')  # cancelled or otherwise finished
        self.assertEqual(qpet.player.current, 'idle')
        self.assertEqual(qpet.player.default_animation, 'idle')
        qpet.player.advance()
        self.assertEqual(qpet.player.current, 'idle')
        manager.play_action('work')
        with mock.patch.object(codex_pet.QTimer, 'singleShot'):
            qpet.greet()
        manager.play_action('celebrate')
        self.assertEqual(qpet.player.current, '18')
        qpet.player.advance()
        self.assertEqual(qpet.player.current, 'idle')

    def test_click_and_context_greetings_while_working_resume_work(self):
        manager = self.manager
        pet = manager.qpet
        manager.play_action('work')

        class TestMenu(codex_pet.QMenu):
            def exec_(self, _position):
                next(action for action in self.actions() if action.text() == '打招呼').trigger()

        with mock.patch.object(manager, 'on_user_greet'):
            QTest.mouseClick(pet, Qt.LeftButton, pos=QPoint(50, 50))
            QTest.qWait(QApplication.doubleClickInterval() + 250)
            self.assertTrue(pet.speech.isVisible())
            self.assertEqual(pet.player.current, 'work02')
            self.assertEqual(manager.on_user_greet.call_count, 1)
            with mock.patch.object(codex_pet, 'QMenu', TestMenu):
                pet.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(10, 10)))
            self.assertEqual(manager.on_user_greet.call_count, 2)
            self.assertEqual(pet.player.current, '01')
            pet.player.advance()
            self.assertEqual(pet.player.current, 'work02')

    def test_work_status_change_during_drag_or_landing_uses_latest_state(self):
        manager = self.manager
        pet = manager.qpet
        start, end = QPoint(30, 30), QPoint(70, 60)
        QTest.mousePress(pet, Qt.LeftButton, pos=start)
        move = QMouseEvent(QMouseEvent.MouseMove, QPointF(end), QPointF(pet.mapToGlobal(end)),
                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(pet, move)
        self.assertEqual(pet.player.current, 'drag')
        manager.play_action('work')
        self.assertEqual(pet.player.current, 'drag')
        QTest.mouseRelease(pet, Qt.LeftButton, pos=end)
        self.assertEqual(pet.player.current, 'landing')
        pet.player.advance()
        self.assertEqual(pet.player.current, 'work02')

        QTest.mousePress(pet, Qt.LeftButton, pos=start)
        move = QMouseEvent(QMouseEvent.MouseMove, QPointF(end), QPointF(pet.mapToGlobal(end)),
                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(pet, move)
        QTest.mouseRelease(pet, Qt.LeftButton, pos=end)
        self.assertEqual(pet.player.current, 'landing')
        manager.play_action('idle')
        self.assertEqual(pet.player.current, 'landing')
        pet.player.advance()
        self.assertEqual(pet.player.current, 'idle')

    def test_hidden_q_status_updates_never_restart_timer_until_shown(self):
        manager = self.manager
        pet = manager.qpet
        pet.hide()
        self.assertFalse(pet.player.timer.isActive())
        manager.play_action('work')
        self.assertEqual(pet.player.current, 'work02')
        self.assertFalse(pet.player.timer.isActive())
        manager.play_action('idle')
        self.assertEqual(pet.player.current, 'idle')
        self.assertFalse(pet.player.timer.isActive())
        manager.play_action('celebrate')
        self.assertEqual(pet.player.current, '18')
        self.assertFalse(pet.player.timer.isActive())
        pet.show()
        self.assertTrue(pet.player.timer.isActive())
        self.assertEqual(pet.player.current, '18')

    def test_double_click_syncs_task_state_changed_while_q_is_pressed(self):
        manager = self.manager
        pet = manager.qpet
        with mock.patch.object(manager, 'open_floating'):
            QTest.mousePress(pet, Qt.LeftButton, pos=QPoint(30, 30))
            manager.play_action('work')
            self.assertEqual(pet.player.current, 'idle')
            QTest.mouseDClick(pet, Qt.LeftButton, pos=QPoint(30, 30))
            self.assertEqual(pet.player.current, 'work02')
            QTest.mouseRelease(pet, Qt.LeftButton, pos=QPoint(30, 30))

            QTest.mousePress(pet, Qt.LeftButton, pos=QPoint(30, 30))
            manager.play_action('idle')
            self.assertEqual(pet.player.current, 'work02')
            QTest.mouseDClick(pet, Qt.LeftButton, pos=QPoint(30, 30))
            self.assertEqual(pet.player.current, 'idle')
            QTest.mouseRelease(pet, Qt.LeftButton, pos=QPoint(30, 30))

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
