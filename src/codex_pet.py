#!/usr/bin/python3
"""Interactive, transparent dragon companion for Linux desktops."""
import argparse
import math
from pathlib import Path
import sys
import time

from PIL import Image
from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap, QRadialGradient
from PyQt5.QtWidgets import QApplication, QMenu, QWidget

from pet_behavior import PetBehavior


def asset_path(name):
    directory = Path(__file__).resolve().parent
    installed = directory / name
    return installed if installed.is_file() else directory.parent / 'assets' / name


def motion_frames(path):
    """Gently deform the outer wing and tail regions of the flat illustration.

    The source is not layered: this is a small mesh warp, not skeletal motion.
    The centre and joins remain fixed, and a single complete image is drawn per
    frame so pose changes never overlay two translucent faces.
    """
    with Image.open(path) as source:
        original = source.convert('RGBA')
        original.thumbnail((560, 694), Image.Resampling.LANCZOS)
    width, height = original.size
    frames = []
    for index in range(12):
        phase = 2 * math.pi * index / 12

        def point(x, y):
            nx, ny = x / width, y / height
            wing = max(0.0, (.34 - nx) / .34, (nx - .66) / .34)
            wing *= math.sin(math.pi * (ny - .12) / .34) if .12 < ny < .46 else 0
            tail = max(0.0, (nx - .70) / .30)
            tail *= math.sin(math.pi * (ny - .48) / .46) if .48 < ny < .94 else 0
            return (x + width * .012 * tail * math.sin(phase + .8),
                    y + height * .018 * wing * math.sin(phase)
                    + height * .008 * tail * math.sin(phase + .8))

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
    WIDTH = 380
    HEIGHT = 550
    MESSAGES = ('你好呀！', '摸摸頭，謝謝你！', '今天也一起加油 ✨', '我在這裡陪你。')

    def __init__(self, manager=None):
        super().__init__()
        self.manager = manager
        self.scale_factor = 0.63
        self.frames = {}
        for pose, filename in (('idle', 'dragon-lady.png'), ('blink', 'blink.png'), ('wave', 'wave.png'), ('wave2', 'wave2.png')):
            self.frames[pose] = motion_frames(asset_path(filename))
        self.setWindowTitle('Codex Pet')
        self.setFixedSize(round(self.WIDTH * self.scale_factor), round(self.HEIGHT * self.scale_factor))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setToolTip('點擊互動 · 拖動移動 · 右鍵選單')
        self.wayland = QApplication.platformName().lower().startswith('wayland')
        self.behavior = PetBehavior(time.monotonic(), patrol=False)
        self.press_global = None
        self.drag_offset = None
        self.drag_moved = False
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
                self.react()
            self.press_global = None
            self.drag_offset = None
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.react('很高興見到你！')
            self.press_global = None
            self.drag_offset = None
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction('打招呼', self.react)
        menu.addAction('重播出場動畫', self.replay)
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

    def react(self, message=None):
        # QAction.triggered may pass a bool; it is not a greeting.
        if not isinstance(message, str):
            message = self.behavior.rng.choice(self.MESSAGES)
        self.behavior.react(time.monotonic(), message)
        self.update()

    def set_scale(self, factor):
        self.scale_factor = factor
        self.setFixedSize(round(self.WIDTH * factor), round(self.HEIGHT * factor))
        self.update()

    def replay(self):
        self.behavior.replay(time.monotonic())
        self.update()

    @staticmethod
    def pen(color, width):
        return QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)

    def paintEvent(self, event):
        now = time.monotonic()
        elapsed = now - self.behavior.started
        appear = min(1.0, max(0.0, (elapsed - 0.6) / 2.2))
        glow_strength = max(0.0, 1.0 - max(0.0, elapsed - 2.8) / 1.0)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.scale(self.scale_factor, self.scale_factor)
        if glow_strength > 0:
            glow = QRadialGradient(QPointF(190, 235), 190)
            glow.setColorAt(0, QColor(95, 145, 255, int(85 * glow_strength)))
            glow.setColorAt(0.5, QColor(120, 100, 255, int(45 * glow_strength)))
            glow.setColorAt(1, QColor(60, 70, 180, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QRectF(0, 40, 380, 390))
            p.save()
            p.translate(190, 235)
            p.rotate(elapsed * 35)
            for i, color in enumerate(('#93eaff', '#a795ff', '#e9ddff')):
                p.setPen(self.pen(color, 2.5 - i * .5))
                p.setOpacity(glow_strength * .8)
                radius = 116 + i * 17
                p.drawArc(QRectF(-radius, -radius, radius * 2, radius * 2),
                          int(i * 112 * 16), int((70 + i * 12) * 16))
            p.restore()

        pose = self.behavior.pose(now)
        frame = self.frames[pose][int(now / .2) % 12]
        bounds = frame.size().scaled(350, 465, Qt.KeepAspectRatio)
        x = (self.WIDTH - bounds.width()) / 2
        y = (self.HEIGHT - bounds.height()) / 2 + math.sin(now * 1.5) * 3
        target = QRectF(x, y, bounds.width(), bounds.height())
        p.setOpacity(appear)
        p.drawPixmap(target, frame, QRectF(frame.rect()))
        p.setOpacity(1.0)
        p.setPen(Qt.NoPen)
        for i, (x, y) in enumerate(((50, 120), (331, 155), (39, 305), (340, 335), (89, 420))):
            twinkle = .3 + .7 * (0.5 + 0.5 * math.sin(now * 2.0 + i * 2.1))
            p.setBrush(QColor(190, 227, 255, int(180 * appear * twinkle)))
            radius = 1.5 + twinkle
            p.drawEllipse(QPointF(x, y + math.sin(now + i) * 3), radius, radius)
        if now < self.behavior.reaction_until:
            p.setOpacity(1.0)
            p.setPen(self.pen('#d8c4ff', 1.5))
            p.setBrush(QColor(33, 25, 65, 230))
            p.drawRoundedRect(QRectF(48, 8, 284, 48), 18, 18)
            p.setPen(QColor('#fff8ff'))
            font = QFont()
            font.setPointSize(11)
            p.setFont(font)
            p.drawText(QRectF(54, 8, 272, 48), Qt.AlignCenter, self.behavior.message)
        p.end()



class QPet(QWidget):
    """Independent draggable window for the locally installed Q character."""
    def __init__(self, manager, root):
        super().__init__()
        from sprite_player import SpritePlayer
        self.manager = manager
        self.scale_factor = 1.0
        self.setWindowTitle('Codex Pet · 白龍')
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setToolTip('點擊互動 · 拖動移動 · 右鍵選單')
        self.wayland = QApplication.platformName().lower().startswith('wayland')
        self.press_global = self.drag_offset = None
        self.drag_moved = False
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
        if time.monotonic() < self.message_until:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor('#d8c4ff'), 1))
            painter.setBrush(QColor(33, 25, 65, 230))
            bubble = QRectF(10, 4, self.width() - 20, 28)
            painter.drawRoundedRect(bubble, 12, 12)
            painter.setPen(QColor('#fff8ff'))
            painter.drawText(bubble, Qt.AlignCenter, self.message)

    def respond(self, message):
        if self.player.play('01'):
            self.message = message
            self.message_until = time.monotonic() + 2.4
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
                self.player.play('drag')
                if self.wayland and self.windowHandle():
                    self.windowHandle().startSystemMove()
            if self.drag_moved and not self.wayland:
                self.move(event.globalPos() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.press_global is not None:
            self.player.play('landing' if self.drag_moved else '01')
            self.press_global = self.drag_offset = None
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
        super().hideEvent(event)

    def showEvent(self, event):
        if self.player.source and not self.player.timer.isActive():
            self.player.timer.start(20)
        super().showEvent(event)

    def closeEvent(self, event):
        self.player.close()
        super().closeEvent(event)


class PetManager:
    """Keep the characters independent while sharing one conversation panel."""
    def __init__(self):
        from import_bailong import default_destination
        self.panel = None
        self.dragon = CodexPet(self)
        self.qpet = None
        root = default_destination()
        if root.exists():
            try:
                self.qpet = QPet(self, root)
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

    def interaction_ready(self, allow_dragon_reaction=False):
        dragon, qpet = self.dragon, self.qpet
        # Qt cannot reliably establish absolute window positions on Wayland.
        if (not qpet or dragon.wayland or qpet.wayland or not dragon.isVisible()
                or not qpet.isVisible() or dragon.press_global is not None
                or qpet.press_global is not None or qpet.player.current != 'idle'
                or (not allow_dragon_reaction and time.monotonic() < dragon.behavior.reaction_until)):
            return False
        a, b = dragon.frameGeometry(), qpet.frameGeometry()
        dx = max(a.left() - b.right() - 1, b.left() - a.right() - 1, 0)
        dy = max(a.top() - b.bottom() - 1, b.top() - a.bottom() - 1, 0)
        return math.hypot(dx, dy) <= 200

    def maybe_interact(self):
        now = time.monotonic()
        if now < self.next_interaction:
            return
        if not self.interaction_ready():
            self.next_interaction = now + 10.0
            return
        greeting, reply = self.dragon.behavior.rng.choice((
            ('小白龍，一起加油！', '好呀，一起！'),
            ('今天也有你陪伴呢。', '我在這裡！'),
            ('準備好一起出發了嗎？', '準備好了！'),
        ))
        self.dragon.react(greeting)
        self.next_interaction = now + self.dragon.behavior.rng.uniform(35.0, 55.0)

        def reply_if_ready():
            if (self.dragon.behavior.message == greeting
                    and self.interaction_ready(allow_dragon_reaction=True)):
                self.qpet.respond(reply)

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
        if self.qpet:
            ident = {'work': 'work02', 'celebrate': '18', 'quota': 'quota'}.get(action, 'idle')
            self.qpet.player.play(ident)

    def open_panel(self, page='chat'):
        if self.panel is None:
            from pet_panel import PetPanel
            self.panel = PetPanel(action_callback=self.play_action)
        self.panel.open_page(page)
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

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
