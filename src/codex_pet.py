#!/usr/bin/python3
"""Interactive, transparent dragon companion for Linux desktops."""
import argparse
from datetime import datetime
import math
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw, ImageFilter
from PyQt5.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication, QMenu, QWidget

from pet_behavior import PetBehavior


def asset_path(name):
    directory = Path(__file__).resolve().parent
    installed = directory / name
    return installed if installed.is_file() else directory.parent / 'assets' / name


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


def motion_frames(path):
    """Gently deform the outer wing and tail regions of the flat illustration.

    The source is not layered: this is a small mesh warp, not skeletal motion.
    The centre and joins remain fixed, and a single complete image is drawn per
    frame so pose changes never overlay two translucent faces.
    """
    if isinstance(path, Image.Image):
        original = path.convert('RGBA')
    else:
        with Image.open(path) as source:
            original = source.convert('RGBA')
    original.thumbnail((560, 694), Image.Resampling.LANCZOS)
    width, height = original.size
    frames = []
    for index in range(12):
        phase = 2 * math.pi * index / 12

        def point(x, y):
            nx, ny = x / width, y / height
            wing = max(0.0, (.375 - nx) / .375, (nx - .66) / .34)
            wing *= math.sin(math.pi * (ny - .12) / .34) if .12 < ny < .46 else 0
            tail = max(0.0, (nx - .70) / .30)
            tail *= math.sin(math.pi * (ny - .48) / .46) if .48 < ny < .94 else 0
            return (x + width * .055 * tail * math.sin(phase + .8),
                    y + height * .10 * wing * math.sin(phase)
                    + height * .035 * tail * math.sin(phase + .8))

        mesh = []
        xs = [round(width * n / 16) for n in range(17)]
        ys = [round(height * n / 24) for n in range(25)]
        for x0, x1 in zip(xs, xs[1:]):
            for y0, y1 in zip(ys, ys[1:]):
                quad = (*point(x0, y0), *point(x0, y1),
                        *point(x1, y1), *point(x1, y0))
                mesh.append(((x0, y0, x1, y1), quad))
        warped = original.transform(original.size, Image.Transform.MESH, mesh,
                                    resample=Image.Resampling.BICUBIC)
        data = warped.tobytes()
        image = QImage(data, width, height, width * 4, QImage.Format_RGBA8888).copy()
        frames.append(QPixmap.fromImage(image))
    return frames


class CodexPet(QWidget):
    WIDTH = 480
    HEIGHT = 300
    MESSAGES = ('你好呀！', '摸摸頭，謝謝你！', '今天也一起加油 ✨', '我在這裡陪你。')

    def __init__(self, manager=None):
        super().__init__()
        from speech_bubble import SpeechBubble
        self.manager = manager
        self.speech = SpeechBubble()
        self.scale_factor = 0.63
        self.frames = {}
        for pose, filename in (('idle', 'dragon-seated.png'), ('blink', 'dragon-seated-blink.png'),
                               ('wave', 'dragon-seated-wave.png'), ('tea', 'dragon-seated-tea.png'),
                               ('happy', 'dragon-seated-happy.png'), ('work', 'dragon-seated-work.png')):
            self.frames[pose] = motion_frames(asset_path(filename))
        self.frames['wave2'] = motion_frames(wave_alternate(
            asset_path('dragon-seated-wave.png'), asset_path('dragon-seated-wave2.png')))
        self.setWindowTitle('Codex Pet')
        self.setFixedSize(round(self.WIDTH * self.scale_factor), round(self.HEIGHT * self.scale_factor))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setToolTip('点击互动 · 双击打开快捷面板 · 拖动移动')
        self.wayland = QApplication.platformName().lower().startswith('wayland')
        self.behavior = PetBehavior(time.monotonic(), patrol=False)
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
        dx = self.behavior.tick(time.monotonic())
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
                self.behavior.begin_drag()
                if self.wayland and self.windowHandle():
                    self.windowHandle().startSystemMove()
            if self.drag_moved and not self.wayland:
                self.move(event.globalPos() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.press_global is not None:
            if self.drag_moved:
                self.behavior.end_drag(time.monotonic())
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
                self.react('很高興見到你！')
            self.press_global = None
            self.drag_offset = None
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction('打招呼', self.react)
        patrol = menu.addAction('桌面巡遊（左右移動）')
        patrol.setCheckable(True)
        patrol.setChecked(self.behavior.patrol)
        patrol.setEnabled(not self.wayland)
        patrol.toggled.connect(lambda enabled: self.behavior.set_patrol(enabled, time.monotonic()))
        if self.wayland:
            menu.addAction('Wayland 不支援自動定位').setEnabled(False)
        if self.manager:
            self.manager.populate_menu(menu, self)
        menu.addSeparator()
        menu.addAction('退出', QApplication.quit)
        menu.exec_(event.globalPos())

    def react(self, message=None, pose='wave'):
        # QAction.triggered may pass a bool; it is not a greeting.
        if not isinstance(message, str):
            message = self.behavior.rng.choice(self.MESSAGES)
        self.behavior.react(time.monotonic(), message, pose)
        if self.isVisible():
            self.speech.show_message(message, self)
        self.update()

    def greet(self):
        self.react(pose='happy')
        if self.manager:
            self.manager.on_user_greet(self)

    def hideEvent(self, event):
        self.speech.hide()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.speech.close()
        super().closeEvent(event)

    def set_scale(self, factor):
        self.scale_factor = factor
        self.setFixedSize(round(self.WIDTH * factor), round(self.HEIGHT * factor))
        self.update()

    def paintEvent(self, event):
        now = time.monotonic()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.scale(self.scale_factor, self.scale_factor)

        # A held pet tilts and floats, then settles after release. This is a
        # single opaque illustration, so drag never produces a ghosted face.
        if self.behavior.dragging:
            p.translate(self.WIDTH / 2, self.HEIGHT / 2)
            p.rotate(4.0 * math.sin(now * 5.0))
            p.translate(-self.WIDTH / 2, -self.HEIGHT / 2 - 8)
        elif self.behavior.landed_at is not None:
            elapsed = now - self.behavior.landed_at
            if 0 <= elapsed < .65:
                p.translate(0, -12 * math.sin(math.pi * elapsed / .65))

        pose = self.behavior.pose(now)
        frame = self.frames[pose][int(now / .2) % 12]
        bounds = frame.size().scaled(470, 266, Qt.KeepAspectRatio)
        x = (self.WIDTH - bounds.width()) / 2
        y = (self.HEIGHT - bounds.height()) / 2 + math.sin(now * 1.5) * 3
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
        self.speech = SpeechBubble()
        self.scale_factor = 1.0
        self.setWindowTitle('Codex Pet · 白龍')
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
        self.player = SpritePlayer(root, self)
        self.player.frame_changed.connect(self.update)
        self.set_scale(1.0)
        area = QApplication.primaryScreen().availableGeometry()
        self.move(max(area.left(), area.right() - 430), max(area.top(), area.bottom() - self.height() - 28))

    def set_scale(self, factor):
        self.scale_factor = factor
        self.setFixedSize(round(180 * factor), round(180 * factor))
        self.update()

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
            self.message = message
            duration = self.speech.show_message(message, self) if self.isVisible() else 0
            self.message_until = time.monotonic() + duration / 1000
            self.update()

    def greet(self):
        # The first catalogue entry is available even in minimal installs.
        import random
        self.respond(random.choice(('我在这里陪你！', '摸摸头，开心！', '今天一起加油呀。', '要不要休息一会儿？')))
        if self.manager:
            self.manager.on_user_greet(self)

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
            else:
                self.click_timer.start(QApplication.doubleClickInterval())
            self.press_global = self.drag_offset = None
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.click_timer.stop()
            self.press_global = self.drag_offset = None
            self.manager.open_floating(self)
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction('打招呼', lambda: self.player.play('01'))
        self.manager.populate_menu(menu, self)
        menu.addSeparator()
        menu.addAction('退出', QApplication.quit)
        menu.exec_(event.globalPos())

    def hideEvent(self, event):
        self.player.timer.stop()
        self.speech.hide()
        super().hideEvent(event)

    def showEvent(self, event):
        if self.player.source and not self.player.timer.isActive():
            self.player.timer.start(20)
        super().showEvent(event)

    def closeEvent(self, event):
        self.player.close()
        self.speech.close()
        super().closeEvent(event)


class _WeatherBridge(QObject):
    received = pyqtSignal(int, str, object)


class PetManager:
    """Keep the characters independent while sharing one conversation panel."""
    def __init__(self):
        from companion import CompanionScheduler, CompanionSettings, CompanionSettingsStore
        from q_idle import QIdleDirector
        from weather import WeatherClient
        from import_bailong import default_destination
        self.panel = None
        self.floating = None
        self.store = CompanionSettingsStore()
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
        self.weather_inflight = False
        self.weather_text = '天气：未设置城市' if not settings.weather_enabled else '天气：等待查询'
        self.weather_detail = ''
        self.dragon = CodexPet(self)
        self.weather_bridge = _WeatherBridge(self.dragon)
        self.weather_bridge.received.connect(self._weather_result)
        self.qpet = None
        root = default_destination()
        if root.exists():
            try:
                self.qpet = QPet(self, root)
                if not self.qpet.wayland:
                    screen = QApplication.screenAt(self.dragon.frameGeometry().center()) or QApplication.primaryScreen()
                    area = screen.availableGeometry()
                    x = max(area.left(), self.dragon.x() - self.qpet.width() - 14)
                    y = max(area.top(), min(self.dragon.y() + self.dragon.height() - self.qpet.height(),
                                            area.bottom() - self.qpet.height() + 1))
                    self.qpet.move(x, y)
            except (OSError, ValueError, ImportError) as exc:
                print(f'白龍素材無法使用：{exc}', file=sys.stderr)
        else:
            print(f'白龍素材尚未導入：codex-pet-import /home/tq/codex-pet.tar.xz（目錄：{root}）', file=sys.stderr)
        self.dragon.show()
        if self.qpet:
            self.qpet.show()
        self.next_interaction = time.monotonic() + 12.0
        self.interaction_timer = QTimer(self.dragon)
        self.interaction_timer.timeout.connect(self.maybe_interact)
        self.interaction_timer.start(1000)
        self.companion_timer = QTimer(self.dragon)
        self.companion_timer.timeout.connect(self.companion_tick)
        self.companion_timer.start(1000)

    def _available(self):
        now = time.monotonic()
        dragon_visible = self.dragon.isVisible()
        q_visible = self.qpet and self.qpet.isVisible()
        return ((dragon_visible or q_visible)
                and (not dragon_visible or (self.dragon.press_global is None
                     and not self.dragon.speech.isVisible()
                     and now >= self.dragon.behavior.reaction_until))
                and (not self.panel or not self.panel.busy)
                and (not q_visible
                     or (self.qpet.press_global is None and self.qpet.player.current == 'idle'
                         and now >= self.qpet.message_until)))

    def companion_tick(self):
        now = time.monotonic()
        event = self.companion.tick(now, datetime.now(), available=self._available())
        if event:
            dragon_visible = self.dragon.isVisible()
            if dragon_visible:
                self.dragon.react(event.message, 'tea' if event.action in ('water', 'break') else 'wave')
            if self.qpet and self.qpet.isVisible() and self.qpet.player.current == 'idle':
                animation = {'break': '14', 'water': '08', 'meal': '17', 'greeting': '20'}.get(event.action, '01')
                if not dragon_visible:
                    self.qpet.respond(event.message, animation)
                    return
                self.qpet.player.play(animation)
                reply = {'break': '我也陪你休息。', 'water': '记得喝点水哦。',
                         'meal': '吃点好吃的吧！', 'greeting': '我也在这里！'}.get(event.action, '我在这里！')

                def q_reply():
                    if (self.companion.settings.enabled and time.monotonic() >= self.companion.paused_until
                            and not self.companion._quiet(datetime.now())
                            and (not self.panel or not self.panel.busy)
                            and self.qpet and self.qpet.isVisible() and self.qpet.press_global is None
                            and self.qpet.player.current in (animation, 'idle')):
                        self.qpet.message = reply
                        duration = self.qpet.speech.show_message(reply, self.qpet)
                        self.qpet.message_until = time.monotonic() + duration / 1000
                        self.qpet.update()

                QTimer.singleShot(900, q_reply)
        if self.qpet:
            self.q_idle.tick(now, self.qpet.isVisible() and self._available(), self.qpet.player)
        # Weather data is a dashboard query, not a proactive interruption.
        # Quiet hours, pause and active work only suppress spoken reminders.
        if self.companion.settings.weather_enabled and now >= self.weather_due and not self.weather_inflight:
            self.refresh_weather()

    def refresh_weather(self):
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
        self._request_weather()
        return True

    def _request_weather(self):
        settings = self.companion.settings
        if not settings.weather_enabled:
            return
        self.weather_inflight = True
        self.weather_due = time.monotonic() + 4 * 3600
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
            if (self.companion.settings.enabled and time.monotonic() >= self.companion.paused_until
                    and self._available() and not self.companion._quiet(datetime.now())):
                if self.dragon.isVisible():
                    self.dragon.react(summary + '，出门记得看天气哦。')
                elif self.qpet and self.qpet.isVisible():
                    self.qpet.respond(summary, '04')
        else:
            self.weather_text = '天气：暂时不可用'
            self.weather_detail = str(data.get('message', '天气查询失败'))
            self.weather_due = time.monotonic() + 5 * 60
        if self.floating:
            self.floating.set_weather(self.weather_text, self.weather_detail)

    def _save_settings(self, settings):
        self.store.save(settings)
        self.companion.set_settings(settings, time.monotonic())
        self.settings_error = None
        self.weather_generation += 1
        self.weather_inflight = False
        self.weather_due = time.monotonic() + 2
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
                          user_initiated=False):
        dragon, qpet = self.dragon, self.qpet
        # Quiet hours, disabled reminders and snooze suppress unsolicited
        # conversation. A response to an explicit click is user-initiated.
        if (not user_initiated and
                (not self.companion.settings.enabled or self.companion._quiet(datetime.now())
                 or time.monotonic() < self.companion.paused_until)):
            return False
        # Qt cannot reliably establish absolute window positions on Wayland.
        if ((self.panel and self.panel.busy)
                or not qpet or dragon.wayland or qpet.wayland or not dragon.isVisible()
                or not qpet.isVisible() or dragon.press_global is not None
                or qpet.press_global is not None
                or qpet.player.current not in (('idle', '01') if allow_q_reaction else ('idle',))
                or (not allow_dragon_reaction and time.monotonic() < dragon.behavior.reaction_until)):
            return False
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
        message = self.dragon.behavior.message if source is self.dragon else self.qpet.message

        def reply_if_ready():
            if source is self.dragon:
                if (self.dragon.behavior.message == message
                        and self.interaction_ready(allow_dragon_reaction=True, user_initiated=True)):
                    self.qpet.respond('我也在，和你一起！')
            elif (self.qpet.message == message
                  and self.interaction_ready(allow_q_reaction=True, user_initiated=True)):
                self.dragon.react('我也在這裡陪你。')

        QTimer.singleShot(1200, reply_if_ready)

    def maybe_interact(self):
        now = time.monotonic()
        if now < self.next_interaction:
            return
        if not self.interaction_ready():
            self.next_interaction = now + 10.0
            return
        greeting, reply, pose, animation = self.dragon.behavior.rng.choice((
            ('小白龍，一起加油！', '好呀，一起！', 'wave', '01'),
            ('今天也有你陪伴呢。', '我在這裡！', 'happy', '20'),
            ('準備好一起出發了嗎？', '準備好了！', 'wave', '04'),
            ('小白龍，我們一起陪伴主人吧。', '嗯！今天也要開開心心。', 'wave', '25'),
            ('累的時候可以歇一歇哦。', '我們一起休息吧！', 'tea', '14'),
            ('小白龍，你今天心情如何？', '看到大家就很開心！', 'happy', '20'),
            ('我們給主人一點鼓勵吧。', '你已經做得很好啦！', 'wave', '01'),
        ))
        self.dragon.react(greeting, pose)
        self.next_interaction = now + self.dragon.behavior.rng.uniform(35.0, 55.0)

        def reply_if_ready():
            if (self.dragon.behavior.message == greeting
                    and self.interaction_ready(allow_dragon_reaction=True)):
                self.qpet.respond(reply, animation if animation in self.qpet.player.entries else '01')

        QTimer.singleShot(900, reply_if_ready)

    def populate_menu(self, menu, current):
        menu.addSeparator()
        menu.addAction('對話與任務', lambda: self.open_panel('chat'))
        menu.addAction('查看額度', lambda: self.open_panel('limits'))
        sizes = menu.addMenu('角色大小')
        for label, value in (('小', 0.75), ('標準', 1.0), ('大', 1.25)):
            # Dragon uses a different base size than Q.
            factor = value * (0.63 if current is self.dragon else 1.0)
            sizes.addAction(label, lambda checked=False, n=factor: current.set_scale(n))
        if self.qpet:
            other = self.qpet if current is self.dragon else self.dragon
            action = menu.addAction('隱藏另一個角色' if other.isVisible() else '顯示另一個角色')
            action.triggered.connect(other.hide if other.isVisible() else other.show)
            menu.addAction('隱藏這個角色', current.hide).setEnabled(other.isVisible())
        else:
            menu.addAction('白龍素材尚未導入（見終端提示）').setEnabled(False)
        if current.wayland:
            menu.addAction('Wayland 下定位及置頂取決於桌面').setEnabled(False)

    def play_action(self, action):
        # A quota refresh can happen while a turn is running. Keep both pets
        # working instead of interrupting their animation with a quota lookup.
        if action == 'quota' and self.dragon.behavior.activity == 'work':
            return
        self.dragon.behavior.set_activity(action, time.monotonic())
        self.dragon.update()
        if self.qpet:
            ident = {'work': 'work02', 'celebrate': '18', 'quota': 'quota'}.get(action, 'idle')
            self.qpet.player.play(ident)

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
