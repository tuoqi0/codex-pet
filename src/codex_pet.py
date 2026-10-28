#!/usr/bin/python3
"""Interactive, transparent dragon companion for Linux desktops."""
import argparse
from dataclasses import replace
from datetime import datetime
import math
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw, ImageFilter
from PyQt5.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QApplication, QMenu, QWidget

from dragon_motion import motion_frames
from pet_behavior import DragMotion, PetBehavior
from input_shape import InputShape, dragon_outline, q_outline


def asset_path(name):
    directory = Path(__file__).resolve().parent
    installed = directory / name
    return installed if installed.is_file() else directory.parent / 'assets' / name



def blink_overlay(base_path, blink_path):
    """Replace the eyes on the same base illustration for a stable blink.

    The supplied full blink image differs slightly in dimensions and resampling
    from the idle image. Restricting it to the eye area avoids a full-body flash.
    """
    with Image.open(base_path) as source:
        base = source.convert('RGBA')
    with Image.open(blink_path) as source:
        blink = source.convert('RGBA').resize(base.size, Image.Resampling.LANCZOS)
    width, height = base.size
    mask = Image.new('L', base.size, 0)
    ImageDraw.Draw(mask).rectangle((round(width * .535), round(height * .150),
                                    round(width * .615), round(height * .230)), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, round(width * .0024))))
    result = Image.composite(blink, base, mask)
    # The source blink asset differs by up to one alpha level in the eye area;
    # preserve the original figure's coverage instead of introducing a fade.
    result.putalpha(base.getchannel('A'))
    return result


def wave_alternate(base_path, alternate_path):
    """Keep the same figure and replace only the raised hand and wrist.

    The two source illustrations are not rigged layers. A feathered local
    patch keeps the face, torso and silhouette fixed between greeting frames.
    """
    with Image.open(base_path) as source:
        base = source.convert('RGBA')
    with Image.open(alternate_path) as source:
        alternate = source.convert('RGBA').resize(base.size, Image.Resampling.LANCZOS)
    width, height = base.size
    mask = Image.new('L', base.size, 0)
    ImageDraw.Draw(mask).rectangle((round(width * .460), round(height * .238),
                                    round(width * .532), round(height * .382)), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, round(width * .004))))
    return Image.composite(alternate, base, mask)


# These source illustrations have a consistent pose outside the changed hand,
# prop, or expression, but their global resampling differs.  Restrict each
# replacement to the area containing the actual gesture so that the stationary
# figure, tail, and wings do not jitter when an action starts or ends.
_GESTURE_REGIONS = {
    'wave': (.40, .21, .57, .59),
    'tea': (.40, .21, .60, .59),
    'happy': (.525, .15, .62, .24),
    'work': (.40, .22, .70, .78),
}

def gesture_overlay(base_path, gesture, region):
    with Image.open(base_path) as source:
        base = source.convert('RGBA')
    if isinstance(gesture, Image.Image):
        alternate = gesture.convert('RGBA').resize(base.size, Image.Resampling.LANCZOS)
    else:
        with Image.open(gesture) as source:
            alternate = source.convert('RGBA').resize(base.size, Image.Resampling.LANCZOS)
    width, height = base.size
    x0, y0, x1, y1 = region
    mask = Image.new('L', base.size)
    ImageDraw.Draw(mask).rectangle((round(width*x0), round(height*y0),
                                    round(width*x1), round(height*y1)), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, round(width * .004))))
    return Image.composite(alternate, base, mask)



class CodexPet(QWidget):
    WIDTH = 480
    HEIGHT = 300
    MESSAGES = ('你好呀！', '摸摸头，谢谢你！', '今天也一起加油 ✨', '我在这里陪你。')

    def __init__(self, manager=None):
        super().__init__()
        from speech_bubble import SpeechBubble
        self.manager = manager
        self.input_shape = InputShape()
        self._shape_screen_connected = False
        self.speech = SpeechBubble()
        self.scale_factor = 0.63
        self.frames = {}
        idle_path = asset_path('dragon-seated.png')
        self.frames['idle'] = motion_frames(idle_path)
        for pose in ('wave', 'tea', 'happy', 'work'):
            self.frames[pose] = motion_frames(gesture_overlay(
                idle_path, asset_path(f'dragon-seated-{pose}.png'), _GESTURE_REGIONS[pose]))
        self.frames['blink'] = motion_frames(blink_overlay(
            asset_path('dragon-seated.png'), asset_path('dragon-seated-blink.png')))
        alternate_wave = wave_alternate(
            asset_path('dragon-seated-wave.png'), asset_path('dragon-seated-wave2.png'))
        self.frames['wave2'] = motion_frames(gesture_overlay(
            idle_path, alternate_wave, _GESTURE_REGIONS['wave']))
        self.setWindowTitle('Codex Pet')
        self.setFixedSize(round(self.WIDTH * self.scale_factor), round(self.HEIGHT * self.scale_factor))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setToolTip('点击互动 · 双击打开快捷面板 · 拖动移动')
        self.wayland = QApplication.platformName().lower().startswith('wayland')
        self.behavior = PetBehavior(time.monotonic(), patrol=False)
        self.drag_motion = DragMotion()
        self.reaction_serial = 0
        self.press_global = None
        self.drag_offset = None
        self.drag_moved = False
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(self.greet)
        self.x_remainder = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.advance)
        self.timer.start(33)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(max(screen.left(), screen.right() - self.width() - 24),
                  max(screen.top(), screen.bottom() - self.height() - 28))

    def advance(self):
        now = time.monotonic()
        dx = self.behavior.tick(now)
        self.drag_motion.advance(now)
        if dx and not self.wayland:
            screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
            area = screen.availableGeometry()
            left = area.left()
            right = max(left, area.right() - self.width() + 1)
            self.x_remainder += dx
            step = int(self.x_remainder)
            self.x_remainder -= step
            desired = self.x() + step
            bounded = max(left, min(right, desired))
            if bounded != desired:
                self.behavior.at_edge()
                self.x_remainder = 0.0
            self.move(bounded, self.y())
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.press_global = event.globalPos()
            self.drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            self.drag_moved = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self.press_global is not None and event.buttons() & Qt.LeftButton:
            if not self.drag_moved and (event.globalPos() - self.press_global).manhattanLength() >= QApplication.startDragDistance():
                self.drag_moved = True
                self.click_timer.stop()
                self.speech.hide()
                # A drag dismisses the current exchange, including any reply
                # still waiting in a single-shot timer.
                self.reaction_serial += 1
                now = time.monotonic()
                self.behavior.begin_drag(now)
                self.drag_motion.begin(now, self.press_global.x())
                if self.wayland and self.windowHandle():
                    self.windowHandle().startSystemMove()
            if self.drag_moved:
                self.drag_motion.move(time.monotonic(), event.globalPos().x())
                if not self.wayland:
                    self.move(event.globalPos() - self.drag_offset)
                self.update()
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.press_global is not None:
            if self.drag_moved:
                now = time.monotonic()
                self.drag_motion.end(now)
                self.behavior.end_drag(now)
                if self.manager:
                    self.manager.window_moved(self)
            else:
                self.click_timer.start(QApplication.doubleClickInterval())
            self.press_global = None
            self.drag_offset = None
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.click_timer.stop()
            if self.manager:
                self.manager.open_floating(self)
            else:
                self.react('很高兴见到你！')
            self.press_global = None
            self.drag_offset = None
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction('打招呼', lambda: self.greet())
        patrol = menu.addAction('桌面巡游（左右移动）')
        patrol.setCheckable(True)
        patrol.setChecked(self.behavior.patrol)
        patrol.setEnabled(not self.wayland)
        if self.manager:
            patrol.toggled.connect(self.manager.set_patrol)
        else:
            patrol.toggled.connect(lambda enabled: self.behavior.set_patrol(enabled, time.monotonic()))
        if self.wayland:
            menu.addAction('Wayland 不支持自动定位').setEnabled(False)
        if self.manager:
            self.manager.populate_menu(menu, self)
        menu.addSeparator()
        menu.addAction('退出', QApplication.quit)
        menu.exec_(event.globalPos())

    def react(self, message=None, pose='wave'):
        # QAction.triggered may pass a bool; it is not a greeting.
        if not isinstance(message, str):
            message = self.behavior.rng.choice(self.MESSAGES)
        self.reaction_serial += 1
        self.behavior.react(time.monotonic(), message, pose)
        if self.isVisible():
            self.speech.show_message(message, self)
        self.update()

    def greet(self):
        self.react(pose='happy')
        if self.manager:
            self.manager.on_user_greet(self)

    def _update_input_shape(self, *_args):
        if self.isVisible():
            dragon_outline(self)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._shape_screen_connected and self.windowHandle():
            self.windowHandle().screenChanged.connect(self._update_input_shape)
            self._shape_screen_connected = True
        self._update_input_shape()

    def hideEvent(self, event):
        self.speech.hide()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.speech.close()
        self.input_shape.close()
        super().closeEvent(event)

    def set_scale(self, factor):
        self.scale_factor = factor
        self.setFixedSize(round(self.WIDTH * factor), round(self.HEIGHT * factor))
        self._update_input_shape()
        self.update()

    def paintEvent(self, event):
        now = time.monotonic()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.scale(self.scale_factor, self.scale_factor)

        # A single illustration follows the pointer with a damped tilt and lift.
        # Shrink slightly during the gesture to keep the wings inside the window.
        p.translate(self.WIDTH / 2, self.HEIGHT / 2 - self.drag_motion.lift)
        p.rotate(self.drag_motion.angle)
        gesture_scale = 1.0 - self.drag_motion.lift * .01
        p.scale(gesture_scale, gesture_scale)
        p.translate(-self.WIDTH / 2, -self.HEIGHT / 2)

        pose = self.behavior.pose(now)
        frame = self.frames[pose][int(now / .1) % len(self.frames[pose])]
        bounds = frame.size().scaled(470, 266, Qt.KeepAspectRatio)
        x = (self.WIDTH - bounds.width()) / 2
        y = ((self.HEIGHT - bounds.height()) / 2 + math.sin(now * 1.5) * 3
             + self.behavior.landing_offset(now))
        target = QRectF(x, y, bounds.width(), bounds.height())
        p.drawPixmap(target, frame, QRectF(frame.rect()))
        p.setOpacity(1.0)
        p.setPen(Qt.NoPen)
        for i, (x, y) in enumerate(((55, 65), (430, 100), (39, 175), (450, 210), (90, 260))):
            twinkle = .3 + .7 * (0.5 + 0.5 * math.sin(now * 2.0 + i * 2.1))
            p.setBrush(QColor(190, 227, 255, int(180 * twinkle)))
            radius = 1.5 + twinkle
            p.drawEllipse(QPointF(x, y + math.sin(now + i) * 3), radius, radius)
        p.end()



class QPet(QWidget):
    """Independent draggable window for the locally installed Q character."""
    def __init__(self, manager, root):
        super().__init__()
        from sprite_player import SpritePlayer
        from speech_bubble import SpeechBubble
        self.manager = manager
        self.input_shape = InputShape()
        self._shape_screen_connected = False
        self.speech = SpeechBubble()
        self.scale_factor = 1.0
        self.setWindowTitle('Codex Pet · 白龙')
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setToolTip('点击互动 · 双击打开快捷面板 · 拖动移动')
        self.wayland = QApplication.platformName().lower().startswith('wayland')
        self.press_global = self.drag_offset = None
        self.drag_moved = False
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(self.greet)
        self.message = ''
        self.message_until = 0.0
        self.reaction_serial = 0
        self.player = SpritePlayer(root, self)
        self.player.pause()  # The top-level widget starts hidden.
        self.player.frame_changed.connect(self.update)
        self.player.frame_changed.connect(self._update_input_shape)
        self.set_scale(1.0)
        area = QApplication.primaryScreen().availableGeometry()
        self.move(max(area.left(), area.right() - 430), max(area.top(), area.bottom() - self.height() - 28))

    def set_scale(self, factor):
        self.scale_factor = factor
        self.setFixedSize(round(180 * factor), round(180 * factor))
        self._update_input_shape()
        self.update()

    def _update_input_shape(self, *_args):
        if self.isVisible():
            q_outline(self)

    def paintEvent(self, event):
        image = self.player.image
        if image is None:
            return
        entry = self.player.entries.get(self.player.current, {})
        scale = entry.get('renderScale', 1.0)
        offset = entry.get('renderOffset', [0, 0])
        if not isinstance(scale, (int, float)) or not math.isfinite(scale):
            scale = 1.0
        if not isinstance(offset, list) or len(offset) != 2 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in offset):
            offset = [0, 0]
        bounds = image.size().scaled(self.width(), self.height(), Qt.KeepAspectRatio)
        w, h = bounds.width() * scale, bounds.height() * scale
        rect = QRectF((self.width() - w) / 2 + offset[0] * self.width(),
                      self.height() - h + offset[1] * self.height(), w, h)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(rect, image, QRectF(image.rect()))

    def respond(self, message, animation='01'):
        if self.player.play(animation):
            self.reaction_serial += 1
            self.message = message
            duration = self.speech.show_message(message, self) if self.isVisible() else 0
            self.message_until = time.monotonic() + duration / 1000
            self.update()
            return True
        return False

    def greet(self):
        # The first catalogue entry is available even in minimal installs.
        import random
        if self.respond(random.choice(('我在这里陪你！', '摸摸头，开心！', '今天一起加油呀。', '要不要休息一会儿？'))) and self.manager:
            self.manager.on_user_greet(self)

    def _sync_activity_loop(self):
        # A task may start or finish while the mouse is held. Temporary
        # animations already return to the latest default when they finish;
        # looping idle/work animations need an explicit update on release.
        if (self.player.current in ('idle', 'work02')
                and self.player.current != self.player.default_animation):
            self.player.play(self.player.default_animation)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.press_global = event.globalPos()
            self.drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            self.drag_moved = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self.press_global is not None and event.buttons() & Qt.LeftButton:
            if not self.drag_moved and (event.globalPos() - self.press_global).manhattanLength() >= QApplication.startDragDistance():
                self.drag_moved = True
                self.click_timer.stop()
                self.speech.hide()
                self.reaction_serial += 1
                self.player.play('drag')
                if self.wayland and self.windowHandle():
                    self.windowHandle().startSystemMove()
            if self.drag_moved and not self.wayland:
                self.move(event.globalPos() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.press_global is not None:
            if self.drag_moved:
                self.player.play('landing')
                if self.manager:
                    self.manager.window_moved(self)
            else:
                self._sync_activity_loop()
                self.click_timer.start(QApplication.doubleClickInterval())
            self.press_global = self.drag_offset = None
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.click_timer.stop()
            self.press_global = self.drag_offset = None
            self._sync_activity_loop()
            self.manager.open_floating(self)
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction('打招呼', lambda: self.greet())
        self.manager.populate_menu(menu, self)
        menu.addSeparator()
        menu.addAction('退出', QApplication.quit)
        menu.exec_(event.globalPos())

    def hideEvent(self, event):
        self.player.pause()
        self.speech.hide()
        super().hideEvent(event)

    def showEvent(self, event):
        self.player.resume()
        super().showEvent(event)
        if not self._shape_screen_connected and self.windowHandle():
            self.windowHandle().screenChanged.connect(self._update_input_shape)
            self._shape_screen_connected = True
        self._update_input_shape()

    def closeEvent(self, event):
        self.player.close()
        self.speech.close()
        self.input_shape.close()
        super().closeEvent(event)


class _WeatherBridge(QObject):
    received = pyqtSignal(int, str, object)


class _ScreenBridge(QObject):
    def __init__(self, manager, parent):
        super().__init__(parent)
        self.manager = manager

    def changed(self, *_args):
        # Qt may update the screen list after delivering screenRemoved.
        QTimer.singleShot(0, self.manager._clamp_windows)

    def added(self, screen):
        screen.availableGeometryChanged.connect(self.changed)
        self.changed()


class PetManager:
    """Keep the characters independent while sharing one conversation panel."""
    def __init__(self):
        from companion import CompanionScheduler, CompanionSettings, CompanionSettingsStore
        from q_idle import QIdleDirector
        from weather import WeatherClient
        from import_bailong import default_destination
        from window_preferences import WindowPreferencesStore
        self.panel = None
        self.floating = None
        self.store = CompanionSettingsStore()
        self.window_store = WindowPreferencesStore()
        self.window_preferences, self.window_warning = self.window_store.load()
        self.settings_error = None
        try:
            settings = self.store.load()
        except (OSError, ValueError) as exc:
            self.settings_error = str(exc)
            settings = CompanionSettings(enabled=False)
        now = time.monotonic()
        self.companion = CompanionScheduler(now, settings)
        self.q_idle = QIdleDirector(now)
        self.weather = WeatherClient()
        self.weather_generation = 0
        self.weather_due = now + 15
        # CLOCK_MONOTONIC excludes suspend on Linux.  The wall deadline makes
        # a refresh due immediately after a long sleep instead of showing an
        # old forecast for another 15 minutes after resume.
        self.weather_wall_due = time.time() + 15
        self.weather_inflight = False
        self.weather_text = '天气：未设置城市' if not settings.weather_enabled else '天气：等待查询'
        self.weather_detail = ''
        self.weather_forecast = None
        self.weather_received_wall = None
        self.weather_seen = set()
        self.dragon = CodexPet(self)
        self.dragon.set_scale(self.window_preferences.dragon.scale)
        self.dragon.behavior.set_patrol(self.window_preferences.dragon_patrol and not self.dragon.wayland, now)
        self._restore_window(self.dragon, self.window_preferences.dragon)
        self.weather_bridge = _WeatherBridge(self.dragon)
        self.weather_bridge.received.connect(self._weather_result)
        self.qpet = None
        root = default_destination()
        if root.exists():
            try:
                self.qpet = QPet(self, root)
                self.qpet.set_scale(self.window_preferences.qpet.scale)
                if not self.qpet.wayland:
                    screen = QApplication.screenAt(self.dragon.frameGeometry().center()) or QApplication.primaryScreen()
                    area = screen.availableGeometry()
                    x = max(area.left(), self.dragon.x() - self.qpet.width() - 14)
                    y = max(area.top(), min(self.dragon.y() + self.dragon.height() - self.qpet.height(),
                                            area.bottom() - self.qpet.height() + 1))
                    self.qpet.move(x, y)
                    self._restore_window(self.qpet, self.window_preferences.qpet)
            except (OSError, ValueError, ImportError) as exc:
                print(f'白龙素材无法使用：{exc}', file=sys.stderr)
        else:
            print(f'白龙素材尚未导入：codex-pet-import <素材压缩包路径>（目录：{root}）', file=sys.stderr)
        from window_preferences import ensure_visible
        available = {'dragon', 'qpet'} if self.qpet else {'dragon'}
        visible_preferences = ensure_visible(self.window_preferences, available)
        self._clamp_windows()
        if visible_preferences.dragon.visible:
            self.dragon.show()
        if self.qpet and visible_preferences.qpet.visible:
            self.qpet.show()
        if self.window_warning:
            anchor = self.dragon if self.dragon.isVisible() else self.qpet
            anchor.speech.show_message(self.window_warning, anchor)
        self._watch_screens()
        self.next_interaction = time.monotonic() + 12.0
        self.next_interaction_speaker = 'dragon'
        self.user_interaction_serial = 0
        self.interaction_timer = QTimer(self.dragon)
        self.interaction_timer.timeout.connect(self.maybe_interact)
        self.interaction_timer.start(1000)
        self.companion_timer = QTimer(self.dragon)
        self.companion_timer.timeout.connect(self.companion_tick)
        self.companion_timer.start(1000)

    @staticmethod
    def _areas():
        return [screen.availableGeometry().getRect() for screen in QApplication.screens()]

    def _restore_window(self, pet, preference):
        from window_preferences import restored_position
        position = restored_position(preference, (pet.width(), pet.height()), self._areas(),
                                     QApplication.platformName())
        if position is not None:
            pet.move(*position)

    def _clamp_pet(self, pet):
        if pet.wayland:
            return
        from window_preferences import PetWindowPreference
        current = PetWindowPreference(pet.x(), pet.y(), 1.0)
        self._restore_window(pet, current)
        screen = QApplication.screenAt(pet.frameGeometry().center()) or QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            base_width, base_height = ((pet.WIDTH, pet.HEIGHT) if pet is self.dragon else (180, 180))
            preferred = (self.window_preferences.dragon.scale if pet is self.dragon
                         else self.window_preferences.qpet.scale)
            factor = min(preferred, area.width() / base_width, area.height() / base_height)
            if pet.scale_factor != factor:
                pet.set_scale(factor)
            current = PetWindowPreference(pet.x(), pet.y(), 1.0)
            self._restore_window(pet, current)

    def _clamp_windows(self):
        self._clamp_pet(self.dragon)
        if self.qpet:
            self._clamp_pet(self.qpet)

    def _watch_screens(self):
        self.screen_bridge = _ScreenBridge(self, self.dragon)
        for screen in QApplication.screens():
            screen.availableGeometryChanged.connect(self.screen_bridge.changed)
        app = QApplication.instance()
        app.screenAdded.connect(self.screen_bridge.added)
        app.screenRemoved.connect(self.screen_bridge.changed)

    def _save_windows(self):
        try:
            previous_backup = self.window_store.backup_path
            self.window_store.save(self.window_preferences)
            self.window_warning = None
            if self.window_store.backup_path != previous_backup:
                anchor = self.dragon if self.dragon.isVisible() else self.qpet
                if anchor and anchor.isVisible():
                    anchor.speech.show_message('原有损坏设置已备份：' + str(self.window_store.backup_path), anchor)
        except (OSError, ValueError) as exc:
            anchor = self.dragon if self.dragon.isVisible() else self.qpet
            if anchor and anchor.isVisible():
                anchor.speech.show_message('无法保存角色窗口设置：' + str(exc), anchor)

    def window_moved(self, pet):
        from window_preferences import remember_position
        if pet.wayland:
            return
        self._clamp_pet(pet)
        name = 'dragon' if pet is self.dragon else 'qpet'
        preference = remember_position(getattr(self.window_preferences, name), pet.x(), pet.y(),
                                       QApplication.platformName())
        self.window_preferences = replace(self.window_preferences, **{name: preference})
        self._save_windows()

    def set_pet_scale(self, pet, factor):
        name = 'dragon' if pet is self.dragon else 'qpet'
        preference = replace(getattr(self.window_preferences, name), scale=factor)
        preference.validate()
        self.window_preferences = replace(self.window_preferences, **{name: preference})
        pet.set_scale(factor)
        self._clamp_pet(pet)
        self._save_windows()

    def set_pet_visible(self, pet, visible):
        from window_preferences import set_visible
        name = 'dragon' if pet is self.dragon else 'qpet'
        available = {'dragon', 'qpet'} if self.qpet else {'dragon'}
        self.window_preferences = set_visible(self.window_preferences, name, visible, available)
        pet.show() if visible else pet.hide()
        self._save_windows()

    def set_patrol(self, enabled):
        self.dragon.behavior.set_patrol(enabled, time.monotonic())
        self.window_preferences = replace(self.window_preferences, dragon_patrol=enabled)
        self._save_windows()

    def _available(self):
        now = time.monotonic()
        dragon_visible = self.dragon.isVisible()
        q_visible = self.qpet and self.qpet.isVisible()
        return ((dragon_visible or q_visible)
                and (not dragon_visible or (self.dragon.press_global is None
                     and not self.dragon.click_timer.isActive()
                     and not self.dragon.speech.isVisible()
                     and now >= self.dragon.behavior.reaction_until))
                and (not self.panel or not self.panel.busy)
                and (not q_visible
                     or (self.qpet.press_global is None and self.qpet.player.current == 'idle'
                         and not self.qpet.click_timer.isActive()
                         and now >= self.qpet.message_until)))

    def companion_tick(self):
        now = time.monotonic()
        event = self.companion.tick(now, datetime.now(), available=self._available())
        if event:
            dragon_visible = self.dragon.isVisible()
            if dragon_visible:
                self.dragon.react(event.message, 'tea' if event.action in ('water', 'break') else 'wave')
            if self.qpet and self.qpet.isVisible() and self.qpet.player.current == 'idle':
                animation = {'break': '01', 'water': '01', 'meal': '17', 'greeting': '20'}.get(event.action, '01')
                if not dragon_visible:
                    self.qpet.respond(event.message, animation)
                    return
                self.qpet.player.play(animation)
                reply = {'break': '我也陪你休息。', 'water': '记得喝点水哦。',
                         'meal': '吃点好吃的吧！', 'greeting': '我也在这里！'}.get(event.action, '我在这里！')
                dragon_serial = self.dragon.reaction_serial
                qpet_serial = self.qpet.reaction_serial

                def q_reply():
                    if (self.companion.settings.enabled and time.monotonic() >= self.companion.paused_until
                            and not self.companion._quiet(datetime.now())
                            and (not self.panel or not self.panel.busy)
                            and self.dragon.isVisible() and self.dragon.press_global is None
                            and not self.dragon.click_timer.isActive()
                            and self.dragon.reaction_serial == dragon_serial
                            and self.qpet and self.qpet.isVisible() and self.qpet.press_global is None
                            and not self.qpet.click_timer.isActive()
                            and self.qpet.reaction_serial == qpet_serial
                            and self.qpet.player.current in (animation, 'idle')):
                        self.qpet.reaction_serial += 1
                        self.qpet.message = reply
                        duration = self.qpet.speech.show_message(reply, self.qpet)
                        self.qpet.message_until = time.monotonic() + duration / 1000
                        self.qpet.update()

                QTimer.singleShot(900, q_reply)
        if self.qpet:
            self.q_idle.tick(now, self.qpet.isVisible() and self._available(), self.qpet.player)
        wall_now = time.time()
        wall_due = wall_now >= self.weather_wall_due
        from weather import location_day_changed
        day_changed = (self.weather_forecast is not None and self.weather_received_wall is not None
                       and location_day_changed(self.weather_forecast[1], self.weather_received_wall, wall_now))
        if day_changed or (wall_due and now < self.weather_due):
            # The machine was likely suspended or the wall clock jumped.
            # Do not speak the pre-suspend forecast while a refresh is due.
            self.weather_forecast = None
        self._weather_reminder(now)
        # Refresh continues during quiet hours; reminders wait until available.
        # Quiet hours, pause and active work only suppress spoken reminders.
        if self.companion.settings.weather_enabled and (now >= self.weather_due or wall_due or day_changed) and not self.weather_inflight:
            self.refresh_weather(announce=False)

    def refresh_weather(self, announce=True):
        """Start a weather refresh if configured; return whether one started."""
        if self.weather_inflight:
            return False
        if not self.companion.settings.weather_enabled:
            self.weather_text = '天气：请在陪伴设置选择城市'
            self.weather_detail = ''
            if self.floating:
                self.floating.set_weather(self.weather_text, self.weather_detail)
            return False
        self.weather_text = '天气：正在查询…'
        self.weather_detail = ''
        if self.floating:
            self.floating.set_weather(self.weather_text, self.weather_detail)
        self._request_weather(announce=announce)
        return True

    def _request_weather(self, announce=True):
        settings = self.companion.settings
        if not settings.weather_enabled:
            return
        self.weather_inflight = True
        self.weather_announce = announce
        self.weather_due = time.monotonic() + 15 * 60
        self.weather_wall_due = time.time() + 15 * 60
        generation = self.weather_generation
        location = {'name': settings.weather_label, 'latitude': settings.weather_latitude,
                    'longitude': settings.weather_longitude}
        self.weather.current(location, lambda event, data: self.weather_bridge.received.emit(generation, event, data))

    def _weather_result(self, generation, event, data):
        if generation != self.weather_generation:
            return
        self.weather_inflight = False
        if event == 'weather':
            settings = self.companion.settings
            summary = f'{settings.weather_label} {data["description"]} {data["temperature"]:g}{data["temperature_unit"]}'
            self.weather_text = '天气：' + summary
            self.weather_detail = f'{data["source"]} · 模型时间 {data["time"]} ({data["timezone"]})'
            self.weather_forecast = (time.monotonic(), data)
            self.weather_received_wall = time.time()
            from weather import tomorrow_warnings
            day, warnings, missing = tomorrow_warnings(data)
            if day is None:
                self.weather_detail += ' · 明日预报不可用'
            else:
                if warnings:
                    self.weather_detail += ' · ' + ' '.join(warnings.values())
                if len(missing) == 5 and not warnings:
                    self.weather_detail += ' · 明日预报不可用'
                elif missing:
                    self.weather_detail += ' · 部分预报不可用：' + '、'.join(sorted(missing))
            if (self.weather_announce and self.companion.settings.enabled and time.monotonic() >= self.companion.paused_until
                    and self._available() and not self.companion._quiet(datetime.now())):
                if self.dragon.isVisible():
                    self.dragon.react(summary + '，出门记得看天气哦。')
                elif self.qpet and self.qpet.isVisible():
                    self.qpet.respond(summary, '04')
        else:
            self.weather_forecast = None
            self.weather_text = '天气：暂时不可用'
            self.weather_detail = str(data.get('message', '天气查询失败'))
            self.weather_due = time.monotonic() + 5 * 60
            self.weather_wall_due = time.time() + 5 * 60
        if self.floating:
            self.floating.set_weather(self.weather_text, self.weather_detail)

    def _weather_reminder(self, now):
        from weather import tomorrow_warnings
        settings = self.companion.settings
        if (not settings.weather_enabled or not settings.weather_alerts or not settings.enabled
                or now < self.companion.paused_until or self.companion._quiet(datetime.now())
                or not self._available() or not self.weather_forecast):
            return
        fetched, data = self.weather_forecast
        if now - fetched > 90 * 60 or now < fetched:
            return
        day, warnings, _missing = tomorrow_warnings(data)
        if not day:
            return
        location = (settings.weather_latitude, settings.weather_longitude)
        self.weather_seen = {key for key in self.weather_seen if key[1] == day}
        pending = [(kind, message) for kind, message in warnings.items()
                   if (location, day, kind) not in self.weather_seen]
        if not pending:
            return
        message = ' '.join(text for _, text in pending)
        if self.dragon.isVisible():
            self.dragon.react(message, 'wave')
        elif self.qpet and self.qpet.isVisible():
            if not self.qpet.respond(message, '01'):
                return
        else:
            return
        self.weather_seen.update((location, day, kind) for kind, _ in pending)

    def _save_settings(self, settings):
        self.store.save(settings)
        self.companion.set_settings(settings, time.monotonic())
        self.settings_error = None
        self.weather_generation += 1
        self.weather_forecast = None
        self.weather_received_wall = None
        self.weather_inflight = False
        self.weather_due = time.monotonic() + 2
        self.weather_wall_due = time.time() + 2
        self.weather_text = '天气：等待查询' if settings.weather_enabled else '天气：未启用'
        self.weather_detail = ''
        if self.floating:
            self.floating.set_weather(self.weather_text)

    def open_settings(self):
        from companion_settings_ui import CompanionSettingsDialog
        if self.settings_error:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self.dragon, '陪伴设置读取失败',
                                '原设置文件已保留，请检查后再保存：' + self.settings_error)
        dialog = CompanionSettingsDialog(self.companion.settings, self._save_settings,
                                        lambda minutes: self.companion.snooze(time.monotonic(), minutes),
                                        parent=self.dragon)
        dialog.exec_()

    def _ensure_panel(self):
        if self.panel is None:
            from pet_panel import PetPanel
            self.panel = PetPanel(action_callback=self.play_action)
            self.panel.limits_changed.connect(self._limits_changed)
        return self.panel

    def _limits_changed(self, event, data):
        if not self.floating:
            return
        if event == 'loading':
            self.floating.set_loading()
        elif event == 'limits':
            self.floating.set_limits(data)
        elif event == 'limits_error':
            self.floating.set_error(data)

    def open_floating(self, pet):
        from pet_floating import PetFloating
        if self.floating is None:
            self.floating = PetFloating(on_action=self._floating_action)
        self.floating.set_weather(self.weather_text, self.weather_detail)
        self._ensure_panel()
        self.floating.open_for(pet)
        if not self.panel.refresh_limits(include_account=False):
            self.floating.set_loading()

    def _floating_action(self, action):
        if action == 'refresh':
            self._ensure_panel().refresh_limits(include_account=False)
        elif action == 'weather':
            self.refresh_weather()
        elif action == 'settings':
            self.open_settings()
        elif action in ('chat', 'plan', 'task'):
            panel = self._ensure_panel()
            panel.mode.setCurrentIndex(panel.mode.findData(action))
            self.floating.hide()
            self.open_panel('chat')

    def interaction_ready(self, allow_dragon_reaction=False, allow_q_reaction=False,
                          user_initiated=False, q_reaction_animation=None):
        dragon, qpet = self.dragon, self.qpet
        # Quiet hours, disabled reminders and snooze suppress unsolicited
        # conversation. A response to an explicit click is user-initiated.
        if (not user_initiated and
                (not self.companion.settings.enabled or self.companion._quiet(datetime.now())
                 or time.monotonic() < self.companion.paused_until)):
            return False
        # Qt cannot reliably establish absolute window positions on Wayland.
        if ((self.panel and self.panel.busy)
                or not qpet or not dragon.isVisible()
                or not qpet.isVisible() or dragon.press_global is not None
                or qpet.press_global is not None
                or dragon.click_timer.isActive() or qpet.click_timer.isActive()
                or qpet.player.current not in (('idle', q_reaction_animation or '01')
                                               if allow_q_reaction else ('idle',))
                or (not allow_dragon_reaction and time.monotonic() < dragon.behavior.reaction_until)):
            return False
        # Do not overwrite a still-visible reminder, weather result or other
        # conversation. The initiating pet's own bubble is allowed for replies.
        if not user_initiated and ((dragon.speech.isVisible() and not allow_dragon_reaction)
                                   or (qpet.speech.isVisible() and not allow_q_reaction)):
            return False
        if dragon.wayland or qpet.wayland:
            # Only an explicit greeting can be answered without reliable
            # absolute positions; unsolicited chat still needs proximity.
            return user_initiated
        a, b = dragon.frameGeometry(), qpet.frameGeometry()
        dx = max(a.left() - b.right() - 1, b.left() - a.right() - 1, 0)
        dy = max(a.top() - b.bottom() - 1, b.top() - a.bottom() - 1, 0)
        return math.hypot(dx, dy) <= 200

    def on_user_greet(self, source):
        """Let the other nearby companion join a user's greeting once."""
        if not self.interaction_ready(allow_dragon_reaction=source is self.dragon,
                                      allow_q_reaction=source is self.qpet,
                                      user_initiated=True):
            return
        self.next_interaction = time.monotonic() + self.dragon.behavior.rng.uniform(35.0, 55.0)
        serial = source.reaction_serial
        target = self.qpet if source is self.dragon else self.dragon
        target_serial = target.reaction_serial

        def reply_if_ready():
            if target.reaction_serial != target_serial:
                return
            if source is self.dragon:
                if (self.dragon.reaction_serial == serial
                        and self.interaction_ready(allow_dragon_reaction=True, user_initiated=True)):
                    self.qpet.respond('我也在，和你一起！')
            elif (self.qpet.reaction_serial == serial
                  and self.interaction_ready(allow_q_reaction=True, user_initiated=True)):
                self.dragon.react('我也在这里陪你。')

        QTimer.singleShot(1200, reply_if_ready)

    def _user_response(self, pet, message, pose, animation):
        if not pet or not pet.isVisible() or pet.press_global is not None:
            return False
        working = (self.dragon.behavior.activity == 'work' or
                   (self.panel and self.panel.busy and self.panel.active_mode in ('plan', 'task')))
        if pet is self.dragon:
            if not working:
                pet.react(message, pose)
                return True
            # A requested response is allowed during a task, but it must not
            # replace the working pose or change the task state.
            pet.reaction_serial += 1
            pet.speech.show_message(message, pet)
            return True
        if not working:
            chosen = animation if animation in pet.player.entries else '01'
            if pet.respond(message, chosen):
                return True
        # Keep work/drag animation intact; also allow a text reply if an
        # optional Q animation is unavailable.
        pet.reaction_serial += 1
        pet.message = message
        pet.message_until = time.monotonic() + pet.speech.show_message(message, pet) / 1000
        return True

    def user_interaction(self, source, action):
        choices = {
            'tea': (('来，一起喝杯茶，歇一会儿。', 'tea'), ('我来准备茶点！', '17')),
            'rest': (('闭上眼睛，一起休息一会儿。', 'blink'), ('我也要打个小盹。', '14')),
            'encourage': (('你已经做得很好了，我们慢慢来。', 'wave'), ('加油！我相信你！', '20')),
            'happy': (('开心一下，让我陪着你！', 'happy'), ('一起开心！', '18')),
        }
        if (source is not self.dragon and (not self.qpet or source is not self.qpet)) or action not in choices:
            return
        if not source.isVisible() or source.press_global is not None:
            return
        # A context-menu action supersedes a pending single click. Otherwise
        # its delayed greeting can overwrite this choice and cancel the reply.
        source.click_timer.stop()
        dragon, qpet = choices[action]
        replies = {self.dragon: (dragon[0], dragon[1], '01')}
        if self.qpet:
            replies[self.qpet] = (qpet[0], 'wave', qpet[1])
        if not self._user_response(source, *replies[source]):
            return
        self.user_interaction_serial += 1
        interaction_serial = self.user_interaction_serial
        source_serial = source.reaction_serial
        other = self.qpet if source is self.dragon else self.dragon
        self.next_interaction = time.monotonic() + self.dragon.behavior.rng.uniform(35.0, 55.0)
        if not other or not other.isVisible():
            return
        other_serial = other.reaction_serial

        def reply_if_ready():
            if (self.user_interaction_serial == interaction_serial and source.isVisible()
                    and other.isVisible() and source.reaction_serial == source_serial
                    and other.reaction_serial == other_serial
                    and source.press_global is None and other.press_global is None
                    and not source.click_timer.isActive() and not other.click_timer.isActive()):
                self._user_response(other, *replies[other])

        QTimer.singleShot(900, reply_if_ready)

    def maybe_interact(self):
        now = time.monotonic()
        if now < self.next_interaction:
            return
        if not self.interaction_ready():
            self.next_interaction = now + 10.0
            return
        if self.next_interaction_speaker == 'dragon':
            greeting, reply, pose, animation = self.dragon.behavior.rng.choice((
                ('小白龙，一起加油！', '好呀，一起！', 'wave', '01'),
                ('今天也有你陪伴呢。', '我在这里！', 'happy', '20'),
                ('准备好一起出发了吗？', '准备好了！', 'wave', '04'),
                ('小白龙，我们一起陪伴主人吧。', '嗯！今天也要开开心心。', 'wave', '20'),
                ('累的时候可以歇一歇哦。', '我来准备点心！', 'tea', '17'),
                ('小白龙，你今天心情如何？', '看到大家就很开心！', 'happy', '20'),
                ('我们给主人一点鼓励吧。', '你已经做得很好啦！', 'wave', '01'),
            ))
            self.dragon.react(greeting, pose)
            serial = self.dragon.reaction_serial
            target_serial = self.qpet.reaction_serial
            self.next_interaction_speaker = 'qpet'

            def reply_if_ready():
                if (self.dragon.reaction_serial == serial
                        and self.qpet.reaction_serial == target_serial
                        and self.interaction_ready(allow_dragon_reaction=True)):
                    self.qpet.respond(reply, animation if animation in self.qpet.player.entries else '01')
        else:
            greeting, reply, animation, pose = self.dragon.behavior.rng.choice((
                ('龙姐姐，你在忙什么呀？', '在这里陪你，一起慢慢来。', '01', 'happy'),
                ('我们一起给主人加油吧！', '好呀，你做得很好哦。', '20', 'wave'),
                ('喝茶配点心，我来准备！', '来，我们歇一小会儿。', '17', 'tea'),
                ('今天也有大家一起，真开心。', '嗯，我也很高兴！', '04', 'happy'),
            ))
            animation = animation if animation in self.qpet.player.entries else '01'
            if not self.qpet.respond(greeting, animation):
                self.next_interaction = now + 10.0
                return
            serial = self.qpet.reaction_serial
            target_serial = self.dragon.reaction_serial
            self.next_interaction_speaker = 'dragon'

            def reply_if_ready():
                if (self.qpet.reaction_serial == serial
                        and self.dragon.reaction_serial == target_serial
                        and self.interaction_ready(allow_q_reaction=True,
                                                   q_reaction_animation=animation)):
                    self.dragon.react(reply, pose)

        self.next_interaction = now + self.dragon.behavior.rng.uniform(35.0, 55.0)
        QTimer.singleShot(900, reply_if_ready)

    def populate_menu(self, menu, current):
        menu.addSeparator()
        interactions = menu.addMenu('一起互动')
        for label, action in (('一起喝茶', 'tea'), ('一起小憩', 'rest'),
                              ('给我鼓励', 'encourage'), ('开心一下', 'happy')):
            interactions.addAction(label, lambda checked=False, value=action: self.user_interaction(current, value))
        menu.addAction('对话与任务', lambda: self.open_panel('chat'))
        menu.addAction('查看额度', lambda: self.open_panel('limits'))
        sizes = menu.addMenu('角色大小')
        for label, value in (('小', 0.75), ('标准', 1.0), ('大', 1.25)):
            # Dragon uses a different base size than Q.
            factor = value * (0.63 if current is self.dragon else 1.0)
            sizes.addAction(label, lambda checked=False, n=factor: self.set_pet_scale(current, n))
        if self.qpet:
            other = self.qpet if current is self.dragon else self.dragon
            action = menu.addAction('隐藏另一个角色' if other.isVisible() else '显示另一个角色')
            action.triggered.connect(lambda: self.set_pet_visible(other, not other.isVisible()))
            menu.addAction('隐藏这个角色', lambda: self.set_pet_visible(current, False)).setEnabled(other.isVisible())
        else:
            menu.addAction('白龙素材尚未导入（见终端提示）').setEnabled(False)
        if current.wayland:
            menu.addAction('Wayland 下定位及置顶取决于桌面').setEnabled(False)

    def play_action(self, action):
        # A quota refresh can happen while a turn is running. Keep both pets
        # working instead of interrupting their animation with a quota lookup.
        if action == 'quota' and self.dragon.behavior.activity == 'work':
            return
        self.dragon.behavior.set_activity(action, time.monotonic())
        self.dragon.update()
        if self.qpet:
            ident = {'work': 'work02', 'celebrate': '18', 'quota': 'quota'}.get(action, 'idle')
            player = self.qpet.player
            player.default_animation = 'work02' if action == 'work' and 'work02' in player.entries else 'idle'
            # A status update can arrive during a mouse gesture. Let the
            # drag and landing finish, then resume the latest activity.
            if self.qpet.press_global is None and player.current != 'landing':
                player.play(ident)

    def open_panel(self, page='chat'):
        panel = self._ensure_panel()
        panel.open_page(page)

def main():
    parser = argparse.ArgumentParser(description='Dragon desktop companion')
    parser.add_argument('--quit-after-ms', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    manager = PetManager()
    if args.quit_after_ms > 0:
        QTimer.singleShot(args.quit_after_ms, app.quit)
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
