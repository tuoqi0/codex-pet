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

    def __init__(self):
        super().__init__()
        self.frames = {}
        for pose, filename in (('idle', 'dragon-lady.png'), ('blink', 'blink.png'), ('wave', 'wave.png'), ('wave2', 'wave2.png')):
            frame = QPixmap(str(asset_path(filename)))
            if frame.isNull():
                raise RuntimeError(f'Cannot load pet pose: {asset_path(filename)}')
            self.frames[pose] = frame
        self.setWindowTitle('Codex Pet')
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setToolTip('點擊互動 · 拖動移動 · 右鍵選單')
        self.wayland = QApplication.platformName().lower().startswith('wayland')
        self.behavior = PetBehavior(time.monotonic(), patrol=not self.wayland)
        self.press_global = None
        self.drag_offset = None
        self.drag_moved = False
        self.x_remainder = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.advance)
        self.timer.start(33)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(max(screen.left(), screen.right() - self.WIDTH - 24),
                  max(screen.top(), screen.bottom() - self.HEIGHT - 28))

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
        menu.addSeparator()
        menu.addAction('退出', QApplication.quit)
        menu.exec_(event.globalPos())

    def react(self, message=None):
        # QAction.triggered may pass a bool; it is not a greeting.
        if not isinstance(message, str):
            message = self.behavior.rng.choice(self.MESSAGES)
        self.behavior.react(time.monotonic(), message)
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

        frame = self.frames[self.behavior.pose(now)]
        bounds = frame.size().scaled(350, 465, Qt.KeepAspectRatio)
        x = (self.WIDTH - bounds.width()) / 2
        y = (self.HEIGHT - bounds.height()) / 2 + math.sin(now * 1.5) * 3
        p.setOpacity(appear)
        p.drawPixmap(QRectF(x, y, bounds.width(), bounds.height()), frame, QRectF(frame.rect()))
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


def main():
    parser = argparse.ArgumentParser(description='Dragon desktop companion')
    parser.add_argument('--quit-after-ms', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    pet = CodexPet()
    pet.show()
    if args.quit_after_ms > 0:
        QTimer.singleShot(args.quit_after_ms, app.quit)
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
