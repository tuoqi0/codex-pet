#!/usr/bin/python3
"""Interactive, transparent dragon companion for Linux desktops."""
import argparse
import math
from pathlib import Path
import sys
import time

from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QRadialGradient
from PyQt5.QtWidgets import QApplication, QMenu, QWidget

from pet_behavior import PetBehavior


def asset_path(name):
    directory = Path(__file__).resolve().parent
    installed = directory / name
    return installed if installed.is_file() else directory.parent / 'assets' / name


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
            frame = QPixmap(str(asset_path(filename)))
            if frame.isNull():
                raise RuntimeError(f'Cannot load pet pose: {asset_path(filename)}')
            self.frames[pose] = frame
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
        self.display_pose = 'idle'
        self.previous_pose = 'idle'
        self.pose_changed_at = time.monotonic()
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
        if pose != self.display_pose:
            self.previous_pose, self.display_pose = self.display_pose, pose
            self.pose_changed_at = now
        frame = self.frames[self.display_pose]
        blend = min(1.0, (now - self.pose_changed_at) / 0.16)
        bounds = frame.size().scaled(350, 465, Qt.KeepAspectRatio)
        x = (self.WIDTH - bounds.width()) / 2
        y = (self.HEIGHT - bounds.height()) / 2 + math.sin(now * 1.5) * 3
        target = QRectF(x, y, bounds.width(), bounds.height())
        previous = self.frames[self.previous_pose]
        p.setOpacity(appear * (1.0 - blend))
        p.drawPixmap(target, previous, QRectF(previous.rect()))
        p.setOpacity(appear * blend)
        p.drawPixmap(target, frame, QRectF(frame.rect()))
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
