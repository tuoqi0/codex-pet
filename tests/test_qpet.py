"""Exercise catalogue playback and archive import without third-party assets."""
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PIL import Image
from PyQt5.QtCore import QPoint, QEvent, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from import_bailong import import_assets
from sprite_player import SpritePlayer
from codex_pet import CodexPet, PetManager, QPet


class QPetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        images = [Image.new('RGBA', (10, 10), color) for color in ('red', 'blue')]
        images[0].save(self.root / 'idle.webp', save_all=True, append_images=images[1:], duration=50, loop=0)
        (self.root / '素材清单.json').write_text(json.dumps({
            'idle': {'id': 'idle', 'file': 'idle.webp'},
            'variants': [{'id': '01', 'file': 'idle.webp'}],
            'workAnimations': [{'id': 'work02', 'file': 'idle.webp'}],
            'dragAnimation': {'id': 'drag', 'file': 'idle.webp'},
            'landingAnimation': {'id': 'landing', 'file': 'idle.webp'},
        }), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_one_shot_and_loop(self):
        player = SpritePlayer(self.root)
        self.assertTrue(player.play('01'))
        player.advance()
        self.assertEqual(player.index, 1)
        player.advance()
        self.assertEqual(player.current, 'idle')
        player.advance()
        player.advance()
        self.assertEqual(player.current, 'idle')
        player.play('work02')
        player.advance()
        player.advance()
        self.assertEqual(player.current, 'work02')
        player.play('idle')
        self.assertEqual(player.current, 'idle')
        player.close()

    def test_window_scales_and_releases_decoder(self):
        pet = QPet(None, self.root)
        pet.show()
        pet.set_scale(0.75)
        self.assertEqual((pet.width(), pet.height()), (135, 135))
        pet.hide()
        self.assertFalse(pet.player.timer.isActive())
        pet.show()
        self.assertTrue(pet.player.timer.isActive())
        pet.close()
        self.assertIsNone(pet.player.source)

    def test_single_click_greets_after_double_click_interval(self):
        manager = mock.Mock()
        pet = QPet(manager, self.root)
        pet.show()
        with mock.patch.object(pet.player, 'play', wraps=pet.player.play) as play:
            QTest.mouseClick(pet, Qt.LeftButton, pos=QPoint(70, 70))
            self.assertTrue(pet.click_timer.isActive())
            play.assert_not_called()
            QTest.qWait(QApplication.doubleClickInterval() + 100)
            play.assert_any_call('01')
            manager.open_floating.assert_not_called()
        pet.close()

    def test_double_click_opens_panel_and_cancels_single_click(self):
        manager = mock.Mock()
        pet = QPet(manager, self.root)
        pet.show()
        pos = QPoint(70, 70)
        with mock.patch.object(pet.player, 'play', wraps=pet.player.play) as play:
            QTest.mouseClick(pet, Qt.LeftButton, pos=pos)
            self.assertTrue(pet.click_timer.isActive())
            QTest.mouseDClick(pet, Qt.LeftButton, pos=pos)
            QTest.mouseRelease(pet, Qt.LeftButton, pos=pos)
            manager.open_floating.assert_called_once_with(pet)
            self.assertFalse(pet.click_timer.isActive())
            QTest.qWait(QApplication.doubleClickInterval() + 100)
            play.assert_not_called()
        pet.close()

    def test_drag_plays_landing_without_greeting_or_opening_panel(self):
        manager = mock.Mock()
        pet = QPet(manager, self.root)
        pet.show()
        with mock.patch.object(pet.player, 'play', wraps=pet.player.play) as play:
            QTest.mousePress(pet, Qt.LeftButton, pos=QPoint(70, 70))
            pos = QPoint(110, 100)
            move = QMouseEvent(QEvent.MouseMove, pos, pet.mapToGlobal(pos), Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
            QApplication.sendEvent(pet, move)
            self.assertEqual(pet.player.current, 'drag')
            QTest.mouseRelease(pet, Qt.LeftButton, pos=pos)
            self.assertEqual(pet.player.current, 'landing')
            QTest.qWait(QApplication.doubleClickInterval() + 100)
            self.assertNotIn(mock.call('01'), play.call_args_list)
            manager.open_floating.assert_not_called()
        pet.close()

    def test_drag_cancels_a_pending_single_click(self):
        manager = mock.Mock()
        pet = QPet(manager, self.root)
        pet.show()
        with mock.patch.object(pet.player, 'play', wraps=pet.player.play) as play:
            start, end = QPoint(70, 70), QPoint(110, 100)
            QTest.mouseClick(pet, Qt.LeftButton, pos=start)
            self.assertTrue(pet.click_timer.isActive())
            QTest.mousePress(pet, Qt.LeftButton, pos=start)
            move = QMouseEvent(QEvent.MouseMove, end, pet.mapToGlobal(end), Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
            QApplication.sendEvent(pet, move)
            QTest.mouseRelease(pet, Qt.LeftButton, pos=end)
            QTest.qWait(QApplication.doubleClickInterval() + 100)
            self.assertNotIn(mock.call('01'), play.call_args_list)
            manager.open_floating.assert_not_called()
        pet.close()

    def test_companions_exchange_greetings_only_when_available(self):
        from companion import CompanionScheduler, CompanionSettings
        manager = PetManager.__new__(PetManager)
        manager.companion = CompanionScheduler(time.monotonic(), CompanionSettings(quiet_start='00:00', quiet_end='00:00'))
        manager.panel = None
        manager.dragon = CodexPet(manager)
        manager.qpet = QPet(manager, self.root)
        dragon, pet = manager.dragon, manager.qpet
        dragon.show()
        pet.show()
        dragon.move(20, 100)
        pet.move(260, 100)
        self.app.processEvents()
        manager.next_interaction = 0
        with mock.patch('codex_pet.QTimer.singleShot') as delayed:
            manager.maybe_interact()
            self.assertTrue(dragon.behavior.message)
            self.assertEqual(pet.player.current, 'idle')
            self.assertEqual(delayed.call_args.args[0], 900)
            reply = delayed.call_args.args[1]
            reply()
            self.assertEqual(pet.player.current, '01')
            self.assertTrue(pet.message)

            pet.player.play('idle')
            pet.hide()
            pet.message = ''
            reply()
            self.assertEqual(pet.message, '')
            pet.show()
            pet.player.play('work02')
            self.assertFalse(manager.interaction_ready(allow_dragon_reaction=True))
            pet.player.play('idle')
            pet.press_global = QPoint(1, 1)
            self.assertFalse(manager.interaction_ready(allow_dragon_reaction=True))
            pet.press_global = None
            pet.move(1000, 100)
            self.assertFalse(manager.interaction_ready(allow_dragon_reaction=True))
            pet.move(260, 100)
            dragon.wayland = True
            self.assertFalse(manager.interaction_ready(allow_dragon_reaction=True))
        dragon.close()
        pet.close()

    def test_import_rejects_path_escape_and_existing_target(self):
        archive = self.root / 'evil.tar.xz'
        with tarfile.open(archive, 'w:xz') as tar:
            data = b'x'
            item = tarfile.TarInfo('codex-pet/user-assets/bailong/../../escape.webp')
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
        with self.assertRaises(ValueError):
            import_assets(archive, self.root / 'output')
        self.assertFalse((self.root / 'escape.webp').exists())
        with self.assertRaises(FileExistsError):
            import_assets(archive, self.root)

    def test_player_rejects_path_escape(self):
        (self.root / '素材清单.json').write_text(json.dumps({'idle': {'id': 'idle', 'file': '../escape.webp'}}), encoding='utf-8')
        with self.assertRaises(ValueError):
            SpritePlayer(self.root)


if __name__ == '__main__':
    unittest.main()
